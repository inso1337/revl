"""A5 is residue-by-design, and G4 carries the obligation (issue #2114).

`formal/STATUS.md` carried **A5** ("compensation accompanies an emission") as
`none` with "Unbuilt work" in its Notes and named it in the summary as one of
three codes with no theorem. A3 and T1-T3 are out of scope by kind, so their
`none` is a classification; A5's was the residue — and it had no open issue.

Issue #2114 defines two exits and this branch takes the second: the honest
reclassification, with the residue NAMED. A5 is a **lowering permission**, not
a refusal rule. `compensate` is an *optional* slot (`DESIGN.md` §3.5 — an
emission "may declare" one), the lowering admits a bare emission
(`src/revl/lower.py`: "emissions are permitted bare (A5)") and carries a
declared clause into the same `emit` step, and `docs/contract-errata.md`
records A5 as an IR v1 amendment rather than a rejection. There is therefore no
A5-shaped verdict for a corpus to flip and no A5 differential row is possible.

The one rule that makes compensation **required** is a *registry* property
rather than A5's — the registry-owned `compensatable` class (roadmap item 522,
`docs/design/538-ui-transactions.md` line 90) — and it refuses under **G4**
(`src/revl/parser.py`, `src/revl/ui_family.py::teardown_refusal`). Crediting
that refusal to A5 would be a false theorem, so **G4 carries the obligation**
and A5's row says so instead of claiming a proof.

What this module pins, all of it cheap and none of it Lean:

  * the checker raises no `A5` code anywhere, with a positive control that the
    same scan DOES find `G4` (so the scan is not vacuous);
  * the only "A5 site" `tools/tier_guarantees.py` can find is a prose tag in a
    comment, which is why its `ACKNOWLEDGED["A5"]` reason is the shape it is;
  * a `compensatable` verb that omits its inverse is refused under `G4`, and an
    ordinary emission keeps both spellings — A5's optionality, measured;
  * `formal/STATUS.md`'s A5 row still places (`revl.cert` reads it), reads
    `none`, says `residue-by-design`, names `G4`, and no longer says
    "Unbuilt work";
  * `docs/guarantees.md`'s A5 row no longer records its origin as
    "unconfirmed", and `docs/rejections.md`'s A5 section names the carrier and
    the registry limit.

This file runs in the plain `pytest tests/` job. `formal/**` carries no CI
gate (`.github/github-app.yml`), so these assertions are the only mechanical
hold on the reclassification.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402
from revl import cert  # noqa: E402
from revl.diagnostics import classify  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402

STATUS = ROOT / "formal" / "STATUS.md"
GUARANTEES = ROOT / "docs" / "guarantees.md"
REJECTIONS = ROOT / "docs" / "rejections.md"


def _parse(source: str):
    return Parser(source, "a5_2114.rvl").parse()


def _refusal(source: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        _parse(source)
    return excinfo.value


def _a5_row() -> dict:
    """A5's row in `formal/STATUS.md`, read by the certificate's own reader.

    `named_guarantee_map` is the reading `revl.cert` carries into every
    component certificate, so a row this test can see is a row the
    certificate can see. Reading the markdown with a local regex instead would
    prove the text exists without proving the layer reads it.
    """
    rows = cert.named_guarantee_map(STATUS.read_text(), source="formal/STATUS.md")
    assert "A5" in rows, sorted(rows)
    return rows["A5"]


def _flatten(text: str) -> str:
    """Whitespace-collapsed, because these documents are hard-wrapped and a
    phrase this test asserts on can straddle a line break."""
    return " ".join(text.split())


def _guarantees_row() -> str:
    for line in GUARANTEES.read_text().splitlines():
        if line.startswith("| [A5]"):
            return line
    pytest.fail("docs/guarantees.md has no A5 row")


def _rejections_section() -> str:
    text = REJECTIONS.read_text()
    start = text.index("## A5 — compensation accompanies an emission")
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


# ------------------------------------------------- the checker emits no A5
#
# The first half of the reclassification's evidence: A5 is not a code this
# compiler refuses under, so a "row" for it could not be a differential row.


def _code_literals(code: str) -> list[str]:
    """Every `code="<code>"` call keyword in `src/revl/`, as `file:line`."""
    hits: list[str] = []
    for path in sorted((ROOT / "src" / "revl").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (keyword.arg == "code"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == code):
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return hits


def test_the_checker_raises_no_a5_code() -> None:
    """No raise site in `src/revl/` carries `code="A5"`, so there is no A5
    refusal for a corpus file to flip and no A5 differential row to add."""
    assert _code_literals("A5") == []


def test_the_same_scan_finds_g4_so_it_is_not_vacuous() -> None:
    """The scan above is only evidence if it finds the codes that ARE raised.
    G4 is the carrier the reclassification names, so it has to show up."""
    assert _code_literals("G4")


def test_a5_is_a_lowering_permission_and_the_lowering_says_so() -> None:
    """A5's own text is in the lowering, as a permission. This is the citation
    `formal/STATUS.md`'s A5 row makes, so the row's evidence is pinned rather
    than quoted."""
    text = (ROOT / "src" / "revl" / "lower.py").read_text()
    assert "emissions are permitted bare (A5)" in text


def test_the_only_a5_site_the_matrix_reader_finds_is_a_prose_tag() -> None:
    """`tools/tier_guarantees.py` reads an `(A5)` message tag as an enforcement
    site. The only one in the tree is the lowering's comment, which is why the
    matrix's A5 cell is `NO_REPRODUCER` and why `ACKNOWLEDGED["A5"]` states the
    reason in prose instead of naming a fixture. A real raise carrying that tag
    would make this fail, which is the point: crediting a refusal to A5 would
    then be a deliberate decision rather than an accident."""
    tg = load_by_path("tier_guarantees", ROOT / "tools" / "tier_guarantees.py")
    sites = tg.enforcement_sites("A5")
    assert "src/revl/lower.py" in sites
    for rel in sites:
        for line in (ROOT / rel).read_text().splitlines():
            if "(A5)" in line:
                assert line.lstrip().startswith("#"), (rel, line)


def test_the_matrix_acknowledgement_names_the_carrier() -> None:
    """The matrix's A5 cell is excused by a reason, and that reason has to say
    what this reclassification says: optional slot, lowering rule, and G4 as
    the code that carries the mandatory shape."""
    tg = load_by_path("tier_guarantees", ROOT / "tools" / "tier_guarantees.py")
    reason = tg.ACKNOWLEDGED["A5"]
    assert "OPTIONAL" in reason
    assert "G4" in reason
    assert "test_ui_transaction_classification_522.py" in reason


# ------------------------------------------------------- G4 is the carrier
#
# The second half: the obligation A5 was read as carrying is real, and it is
# G4's. Both directions are measured, and the control is the one that shows A5
# itself is optional.


def test_a_compensatable_verb_without_its_inverse_is_refused_under_g4() -> None:
    """The mandatory shape. A registry-owned `compensatable` verb that omits
    `compensate` is refused, and the refusal is a G4 `reversibility` finding —
    not an A5 one, because no A5 code exists."""
    record = classify(_refusal(
        "extern emission[ui.text] fn type_into(target: Str, s: Str)\n"
        "  = @py { pass }\n"))
    assert record["code"] == "G4"
    assert record["category"] == "reversibility"


def test_the_mandatory_shape_is_the_registry_and_not_the_grammar() -> None:
    """The same program with an ordinary capability is admitted with and
    without the clause, which is A5's optionality: nothing in the grammar
    requires a compensation, only the registry class does."""
    _parse("extern emission[db.write] fn w(row: Str) = @py { pass }\n")
    _parse("extern emission[db.write] fn w(row: Str) compensate undo_w()"
           " = @py { pass }\n")


# --------------------------------------------------------- the row says so


def test_the_a5_row_reads_none_and_the_certificate_can_place_it() -> None:
    """The row still says `none`, and `revl.cert` can still read it: a status
    cell the certificate cannot place raises, and a certificate that cannot
    report A5 would be the same gap in a new place."""
    row = _a5_row()
    assert cert.status_of(row["status_cell"], source="formal/STATUS.md",
                          code="A5") == cert.UNPROVED
    assert "residue-by-design" in row["status_cell"]


def test_the_a5_row_names_the_carrier_and_not_unbuilt_work() -> None:
    """The reclassification itself: out of scope by kind, the lowering cited,
    and G4 named as the code that carries the obligation."""
    gap = _a5_row()["gap"]
    assert "Out of scope by kind" in gap
    assert "lower.py" in gap
    assert "contract-errata" in gap
    assert "G4 carries the obligation" in gap
    assert "Unbuilt work" not in gap


def test_the_summary_counts_a5_with_the_out_of_scope_codes() -> None:
    """A5 left the "no theorem" list the way A3 and T1-T3 are on it: by kind,
    not by pending work."""
    text = STATUS.read_text()
    bullets = [block for block in text.split("\n- ")
               if "Three guarantee codes have no theorem at all" in block]
    assert len(bullets) == 1, len(bullets)
    bullet = bullets[0]
    for code in ("A3", "A5", "T1-T3"):
        assert code in bullet
    assert "out of scope by kind" in _flatten(bullet)
    assert "G4" in bullet


def test_the_origin_is_recorded_rather_than_unconfirmed() -> None:
    """`docs/guarantees.md` recorded A5's origin as "unconfirmed". Issue #2114
    requires that to stop, and the recorded origin is `DESIGN.md` §3.5 with the
    contract erratum behind it."""
    row = _guarantees_row()
    assert "unconfirmed" not in row
    assert "DESIGN.md" in row
    assert "contract-errata" in row


def test_the_rejections_entry_names_the_carrier_and_the_limit() -> None:
    """`docs/rejections.md` is where a reader looks for "why is there no
    example". The A5 section now answers with the carrier and the one limit
    that is recorded rather than closed."""
    section = _flatten(_rejections_section())
    assert "issue #2114" in section
    assert "out of scope by kind" in section
    assert "G4 carries the obligation" in section
    assert "teardown_refusal" in section
    assert "test_ui_transaction_classification_522.py" in section
    assert "REVERSIBILITY" in section
