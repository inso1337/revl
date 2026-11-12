"""The oracle summary and the agreed-subset line (issue #2739).

Two claims the formal layer used to leave to prose:

  * which rows the differential oracle reaches. The map in `formal/STATUS.md`
    carries one `Oracle` cell per row; the summary below it restated those
    cells by hand and had drifted (it named G5, G6, G8 and G9 as having no
    oracle row while the table beside it said all four had one). The summary
    is now generated from the table by `formal/scripts/oracle_summary.py`, and
    the formal gate runs its `--check`, so the two cannot disagree again.
  * the subset on which the model and the checker are claimed to agree. It was
    the complement of the `out-of-fragment*` buckets, found by reading the
    census. `diff_corpus.py` prints it now, as numbers, from the same buckets
    the gate uses.

These tests pin the generator, the printer and the two wires into the gate;
`make formal` is what runs them over the real document and the real corpus.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _load_by_path import load_by_path  # noqa: E402

SUMMARY = load_by_path("oracle_summary",
                       ROOT / "formal" / "scripts" / "oracle_summary.py")
CORPUS = load_by_path("diff_corpus",
                      ROOT / "formal" / "harness" / "diff_corpus.py")
STATUS = ROOT / "formal" / "STATUS.md"

#: A table shaped like the real one's first four cells, with every verdict the
#: classifier must read: yes, yes with a qualifier, no, and a plain-text row.
_TABLE = """## What the layer covers, and what it does not

| Code | Formal status | Theorems | Oracle | The gap |
|---|---|---|---|---|
| **G1** declared access | partial | 1 | **yes** (one row per component) | gap |
| **G2** provision disjointness | full | 4 | **yes** (V rows, 2 agree-G2) | gap |
| **G7** derived LIFO teardown | full | 1 | **yes** (267 D rows) | gap |
| **G-SECRET** retained data | partial | 1 | **partial** (the same TAINT row) | gap |
| **A3** host-safe identifiers | **none** | 0 | no | gap |
| item 133 cross-tier agreement | full | 4 | no | gap |
"""


def _empty_doc(table: str = _TABLE) -> str:
    return table + (
        "\n<!-- BEGIN GENERATED oracle-summary: regenerate with "
        "`python3 formal/scripts/oracle_summary.py --write` -->\n"
        "<!-- END GENERATED oracle-summary -->\n")


def _written_doc(table: str = _TABLE) -> str:
    return SUMMARY.replace(_empty_doc(table), SUMMARY.block(table))


# ------------------------------------------------ the summary is the table's

def test_the_summary_counts_the_verdicts_the_table_carries():
    block = SUMMARY.block(_TABLE)
    assert "The oracle reaches 3 of the 6 rows" in block
    assert "G1, G2 and G7" in block
    assert "G-SECRET" in block and "partial" in block
    assert "A3 and item 133 cross-tier agreement" in block


def test_a_verdict_that_moves_in_the_table_moves_the_summary():
    before = SUMMARY.block(_TABLE)
    after = SUMMARY.block(_TABLE.replace(
        "| **A3** host-safe identifiers | **none** | 0 | no | gap |",
        "| **A3** host-safe identifiers | **none** | 0 | **yes** | gap |"))
    assert before != after
    assert "The oracle reaches 3 of the 6 rows" in before
    assert "The oracle reaches 4 of the 6 rows" in after
    assert "item 133 cross-tier agreement" in after
    assert "A3 and item 133" not in after


def test_check_accepts_the_block_write_produces():
    assert SUMMARY.check(_written_doc()) is None


def test_check_fails_on_a_summary_that_is_not_the_tables():
    edited = _written_doc().replace(
        "The oracle reaches 3 of the 6 rows", "The oracle reaches 6 of the 6 rows")
    assert edited != _written_doc()
    problem = SUMMARY.check(edited)
    assert problem is not None
    assert "oracle_summary.py --write" in problem


def test_an_unreadable_oracle_cell_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        SUMMARY.block(_TABLE.replace("| 0 | no | gap |", "| 0 | maybe | gap |"))


def test_a_document_without_markers_is_refused():
    with pytest.raises(ValueError):
        SUMMARY.replace(_TABLE, SUMMARY.block(_TABLE))


def test_the_repository_summary_agrees_with_its_table():
    assert SUMMARY.check(STATUS.read_text(encoding="utf-8")) is None


def test_the_formal_gate_runs_the_summary_check():
    gate = (ROOT / "formal" / "scripts" / "run_gate.sh").read_text(encoding="utf-8")
    assert "python3 scripts/oracle_summary.py --check" in gate


# ------------------------------------------------------ the agreed subset

def test_the_agreed_subset_is_the_agree_half_and_the_rest_is_named():
    align = {"agree-accept": 10, "agree-G4": 2, "out-of-fragment": 3,
             "out-of-fragment-G5": 1, "out-of-scope": 4}
    a = CORPUS.agreed_subset(align, parse_refused=5, componentless=6)
    assert sum(a["decided"].values()) == 12
    assert a["refused_by_neither"] == 10
    assert a["no_fact"] == {"out-of-fragment": 3, "out-of-fragment-G5": 1}
    line = CORPUS.format_agreed_subset(a)
    assert line == (
        "agreed subset: 12 corpus documents the model decides and the checker "
        "agrees with (2 refused by the checker, 10 refused by neither side); "
        "the model has no fact about out-of-fragment 3, out-of-fragment-G5 1; "
        "outside the guarantee backbone by explicit rule: out-of-scope 4; "
        "outside the model entirely: 5 parse refusals and 6 componentless "
        "documents")
    assert "\n" not in line, "the census prints one line"


def test_a_disagreement_is_named_in_the_line_not_folded_into_the_subset():
    align = {"agree-accept": 1, "missed-G4": 1, "formal-strict": 1}
    a = CORPUS.agreed_subset(align, 0, 0)
    assert sum(a["decided"].values()) == 1
    line = CORPUS.format_agreed_subset(a)
    assert "DISAGREEING (the gate fails): formal-strict 1, missed-G4 1" in line


def test_the_census_prints_the_agreed_subset():
    source = (ROOT / "formal" / "harness" / "diff_corpus.py").read_text(
        encoding="utf-8")
    assert "print(format_agreed_subset(\n" in source


# ------------------------------------------- G2 over the lowered IR image

TENANTS = "examples/tenants.rvl"


def _facts(rel: str) -> tuple[list[list[str]], list[list[str]]]:
    """The M and JR facts of one corpus file, read the way `export` reads
    them: the model's component facts from the parsed AST, the IR facts from
    the document `check_and_lower` produced. Templates are members of the
    parsed file but not of the lowered document, as in `export`."""
    from revl.lower import check_and_lower
    from revl.parser import Parser

    path = ROOT / rel
    prog = Parser(path.read_text(encoding="utf-8"), str(path)).parse()
    templates = set(CORPUS._spawn_templates(prog))
    members = [c.name for c in prog.components if c.name not in templates]
    lowered = check_and_lower(prog)
    jr = [r.split("\t") for r in CORPUS.ir_fact_rows(rel, lowered, members)]
    # The M rows the oracle consumes, restricted to the columns _ir_reference
    # reads: (kind, file, comp, requires, provides, isolate, role).
    m = [["M", rel, c.name,
          ",".join(local for local, _svc, _ln in c.requires),
          ",".join(key for key, _svc, _ln in c.provides),
          ",".join(f"{k}={v}" for k, v in
                   sorted(CORPUS._isolate_map(c).items())) or "-",
          "template" if c.name in templates else "member"]
         for c in prog.components]
    return m, jr


def _tenants_facts() -> tuple[list[list[str]], list[list[str]]]:
    return _facts(TENANTS)


def test_the_lowering_keeps_the_tenants_slots():
    m, jr = _tenants_facts()
    assert [r[0] for r in jr] == ["JR"] * 4, jr
    disjoint, kept = CORPUS._ir_reference(m, jr)
    assert disjoint == {TENANTS: "ok"}
    assert kept == {TENANTS: "ok"}


def test_a_spawned_component_is_not_a_composition_member_in_either_half():
    # `examples/tenant_attenuation.rvl` spawns its stores, so lowering emits
    # no component entry for them and the model's `comps` excludes them. The
    # reference must exclude them too, or every spawn file reads as drift.
    rel = "examples/tenant_attenuation.rvl"
    m, jr = _facts(rel)
    templates = {r[2] for r in m if r[6] == "template"}
    assert templates, "the fixture must spawn a template"
    assert not any(r[2] in templates for r in jr)
    # The reference must skip those M rows. Without the skip the source side
    # names one component more than the lowered document and the row reads as
    # drift on every spawn file, which is what the first gate run showed.
    _disjoint, kept = CORPUS._ir_reference(m, jr)
    assert kept == {rel: "ok"}
    assert CORPUS._ir_reference(
        [r for r in m if r[6] != "template"], jr)[1] == {rel: "ok"}


def test_a_lost_isolate_entry_fails_the_kept_row():
    m, jr = _tenants_facts()
    # The lowering defect this row exists to catch: the realm is dropped from
    # ONE provider. The slots no longer match the composition the model proved
    # even though the IR is still provision-disjoint.
    mutated = [r[:] for r in jr]
    victim = next(r for r in mutated if r[2] == "TenantBStore")
    victim[4] = "-"
    _disjoint, kept = CORPUS._ir_reference(m, mutated)
    assert kept == {TENANTS: "fail"}


def test_a_lost_isolate_entry_on_both_providers_fails_the_disjoint_row():
    m, jr = _tenants_facts()
    mutated = [r[:] for r in jr]
    for r in mutated:
        if r[2] in ("TenantAStore", "TenantBStore"):
            r[4] = "-"
    disjoint, _kept = CORPUS._ir_reference(m, mutated)
    assert disjoint == {TENANTS: "fail"}


def test_the_ir_rows_are_registered_and_one_sided_with_a_reason():
    kinds = dict(CORPUS.ROW_KINDS)
    assert "IRD" in kinds["ir_disjoint"]
    assert "IRS" in kinds["ir_kept"]
    for field in ("ir_disjoint", "ir_kept"):
        side, why = CORPUS.ONE_SIDED[field]
        assert side == "refused" and why


def test_the_ir_alignment_passes_on_the_tenants_and_fails_on_drift():
    class Ref:
        ir_kept = {TENANTS: "ok"}
        ir_disjoint = {TENANTS: "ok"}
        files = {TENANTS: ("ok", "ok", "ok")}

    assert CORPUS.ir_alignment(Ref()) == []

    class Drift(Ref):
        ir_kept = {TENANTS: "fail"}

    assert CORPUS.ir_alignment(Drift())

    class G2Drift(Ref):
        ir_disjoint = {TENANTS: "fail"}

    findings = CORPUS.ir_alignment(G2Drift())
    assert findings and "IR G2 drift" in findings[0]


def test_ir_coverage_fails_when_no_realm_was_exercised():
    saved = dict(CORPUS._IR_ROWS)
    try:
        CORPUS._IR_ROWS.clear()
        assert CORPUS.ir_coverage()
        CORPUS._IR_ROWS.update({
            "a.rvl": {"components": ["C"], "slots": [("kv", "")],
                      "realms": 0},
        })
        findings = CORPUS.ir_coverage()
        assert any("realm-pinned" in f for f in findings)
    finally:
        CORPUS._IR_ROWS.clear()
        CORPUS._IR_ROWS.update(saved)


def test_the_lean_side_decides_the_ir_rows():
    """The IRD verdict is `decide (RevL.G2.IRDisjoint ..)` and the bridge
    theorem is registered with the axiom gate; the IRS columns are computed by
    `irKeptB`, whose claim is the mutation witness's."""
    oracle = (ROOT / "formal" / "harness" / "Oracle.lean").read_text(
        encoding="utf-8")
    assert "def irdisjointB" in oracle and "theorem irdisjointB_iff" in oracle
    assert "#print axioms RevLOracle.irdisjointB_iff" in oracle
    gate = (ROOT / "formal" / "scripts" / "run_gate.sh").read_text(
        encoding="utf-8")
    assert "RevLOracle.irdisjointB_iff" in gate
    assert "IRD\\t" in oracle and "IRS\\t" in oracle


def test_the_ir_image_theorem_and_its_witness_are_registered():
    check = (ROOT / "formal" / "CheckAxioms.lean").read_text(encoding="utf-8")
    for name in ("RevL.G2.linkOK_ir_disjoint", "RevL.G2.tenants_link_ir",
                 "RevL.G2.tenants_ir_disjoint",
                 "RevL.G2.isolate_entry_is_load_bearing"):
        assert f"#print axioms {name}" in check
    registry = (ROOT / "formal" / "scripts" / "nonvacuity.tsv").read_text(
        encoding="utf-8")
    for name in ("RevL.G2.linkOK_ir_disjoint",
                 "RevL.G2.isolate_entry_is_load_bearing"):
        assert f"{name}\t" in registry
