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

The third claim is G2 over the lowered IR document, the shape the backends
read: the theorem takes the source composition's `LinkOK` and the document
keeping the source's slots, the `IRS` row decides the second, and the `IRD`
row decides disjointness over the document's static composition, whose
templates come from the document's own `manifest.templates`.

These tests pin the generator, the printer and the wires into the gate;
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
#: classifier must read: yes, a yes qualified to one half of the rule (A6's
#: shape, which is NOT a full yes), partial, no, and a plain-text row.
_TABLE = """## What the layer covers, and what it does not

| Code | Formal status | Theorems | Oracle | The gap |
|---|---|---|---|---|
| **G1** declared access | partial | 1 | **yes** (one row per component) | gap |
| **G2** provision disjointness | full | 4 | **yes** (V rows, 2 agree-G2) | gap |
| **G7** derived LIFO teardown | full | 1 | **yes** (267 D rows) | gap |
| **G-SECRET** retained data | partial | 1 | **partial** (the same TAINT row) | gap |
| **A6** provide-methods match | partial | 4 | **yes** for the call-site half (one MS row) | gap |
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
    assert "The oracle reaches 3 of the 7 rows" in block
    assert "in full: G1, G2 and G7" in block
    assert "G-SECRET and A6 carry a **partial** oracle row" in block
    assert "A3 and item 133 cross-tier agreement" in block


def test_a_yes_qualified_to_a_half_is_partial_not_a_full_yes():
    # `**yes** for the call-site half` is A6's cell: the row decides half of
    # the rule, so counting it with the full rows overstated the oracle.
    assert SUMMARY._oracle("**yes** for the call-site half (one MS row)") == "partial"
    assert SUMMARY._oracle("**yes** (V rows, 2 agree-G2)") == "yes"
    assert SUMMARY._oracle("**partial** (the same TAINT row)") == "partial"
    assert SUMMARY._oracle("no") == "no"
    for unreadable in ("**yes**, mostly", "maybe", "**yes** because"):
        with pytest.raises(ValueError):
            SUMMARY._oracle(unreadable)
    rows = dict(SUMMARY.rows(_TABLE))
    assert rows["A6"] == "partial"


def test_the_repository_counts_a6_as_partial():
    rows = dict(SUMMARY.rows(STATUS.read_text(encoding="utf-8")))
    assert rows["A6"] == "partial", rows["A6"]
    assert rows["G-SECRET / G-SECRET-FLOW"] == "partial"


def test_the_executing_rows_are_named_as_running_not_as_comparing_the_checker():
    # G7 drives the reference runtime and A8/R4 the recovery code over an
    # enumerated scenario corpus; none of them is compared with the checker.
    assert SUMMARY.EXECUTING == ("G7", "A8", "R4")
    block = SUMMARY.block(_TABLE)
    assert "for G7 it is the runtime or the recovery code" in block
    assert "compares the model's verdict with the shipped implementation's" in block
    assert "against the shipped checker" not in block
    # only the rows the table counts are named: drop G7 and the clause goes
    no_g7 = _TABLE.replace("**yes** (267 D rows)", "no")
    assert "it is the runtime or the recovery code" not in SUMMARY.block(no_g7)


def test_a_verdict_that_moves_in_the_table_moves_the_summary():
    before = SUMMARY.block(_TABLE)
    after = SUMMARY.block(_TABLE.replace(
        "| **A3** host-safe identifiers | **none** | 0 | no | gap |",
        "| **A3** host-safe identifiers | **none** | 0 | **yes** | gap |"))
    assert before != after
    assert "The oracle reaches 3 of the 7 rows" in before
    assert "The oracle reaches 4 of the 7 rows" in after
    assert "item 133 cross-tier agreement" in after
    assert "A3 and item 133" not in after


def test_check_accepts_the_block_write_produces():
    assert SUMMARY.check(_written_doc()) is None


def test_check_fails_on_a_summary_that_is_not_the_tables():
    edited = _written_doc().replace(
        "The oracle reaches 3 of the 7 rows", "The oracle reaches 7 of the 7 rows")
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
SPAWN = "examples/tenant_attenuation.rvl"


def _lowered(rel: str) -> dict:
    """The document `export` reads: `compile_files`, the door `checker_code`
    uses."""
    from revl.compiler import compile_files

    return compile_files([str(ROOT / rel)])


def _facts(rel: str) -> tuple[list[list[str]], list[list[str]], int]:
    """The M and JR facts of one corpus file, read the way `export` reads
    them: the model's component facts from the parse, the IR facts from the
    lowered document. Returns `(M rows, JR rows, components of other files)`."""
    from revl.parser import Parser

    path = ROOT / rel
    prog = Parser(path.read_text(encoding="utf-8"), str(path)).parse()
    templates = set(CORPUS._spawn_templates(prog))
    jr, foreign = CORPUS.ir_fact_rows(rel, _lowered(rel))
    # The M rows the oracle consumes, restricted to the columns _ir_reference
    # reads: (kind, file, comp, requires, provides, isolate, role).
    m = [["M", rel, c.name,
          ",".join(local for local, _svc, _ln in c.requires),
          ",".join(key for key, _svc, _ln in c.provides),
          ",".join(f"{k}={v}" for k, v in
                   sorted(CORPUS._isolate_map(c).items())) or "-",
          "template" if c.name in templates else "member"]
         for c in prog.components]
    return m, [r.split("\t") for r in jr], foreign


def _tenants_facts():
    m, jr, _foreign = _facts(TENANTS)
    return m, jr


def test_the_lowering_keeps_the_tenants_slots():
    m, jr = _tenants_facts()
    assert [r[0] for r in jr] == ["JR"] * 4, jr
    assert {r[5] for r in jr} == {"member"}
    disjoint, kept = CORPUS._ir_reference(m, jr)
    assert disjoint == {TENANTS: "ok"}
    assert kept == {TENANTS: "ok"}


def test_the_documents_own_templates_list_is_what_excludes_a_spawn_target():
    # `examples/tenant_attenuation.rvl` spawns its two workers. Lowering still
    # LISTS them in `components`, because the backends instantiate them, and
    # names them in `manifest.templates`, which `lower._link` reads to keep them
    # out of the static composition. The role column is read from that list.
    doc = _lowered(SPAWN)
    listed = {c["name"] for c in doc["components"]}
    templates = set(doc["manifest"]["templates"])
    assert templates == {"TenantAWorker", "TenantBWorker"}
    assert templates <= listed, "lowering lists its templates in components"
    m, jr, _foreign = _facts(SPAWN)
    role = {r[2]: r[5] for r in jr}
    assert {n for n, r in role.items() if r == "template"} == templates
    assert role["Router"] == "member"
    # Both workers provide `worker` in one realm: the whole list collides, the
    # static composition does not, and the row agrees with the model.
    assert len([r for r in jr if r[3] == "worker"]) == 2
    disjoint, kept = CORPUS._ir_reference(m, jr)
    assert disjoint == {SPAWN: "ok"}
    assert kept == {SPAWN: "ok"}
    assert CORPUS._IR_ROWS[SPAWN]["template_collision"] is True


def test_reading_the_templates_off_the_parse_instead_would_not_be_this_row():
    # The filter this row used to pass through, `member in AST`, removed the
    # templates before the row saw them. With them in and the document's role
    # column wrong, the slot collision the exclusion hides is the finding.
    m, jr, _foreign = _facts(SPAWN)
    wrong = [r[:5] + ["member"] for r in jr]
    disjoint, kept = CORPUS._ir_reference(m, wrong)
    assert disjoint == {SPAWN: "fail"}
    assert kept == {SPAWN: "fail"}


def test_a_file_with_a_use_is_lowered_through_the_checkers_door():
    # `check_and_lower` on the bare parse refused these files for want of a
    # module directory, and the rows skipped them without a word.
    for rel in ("examples/app/notes.rvl", "examples/app/private_notes.rvl",
                "examples/webui-entry/console.rvl",
                "tests/fixtures/v2_math_main.rvl"):
        m, jr, _foreign = _facts(rel)
        assert jr, rel
        disjoint, kept = CORPUS._ir_reference(m, jr)
        assert kept == {rel: "ok"}, rel


def test_components_of_another_file_are_counted_and_carry_no_row():
    doc = {"components": [
        {"name": "Mine", "source": "examples/tenants.rvl",
         "provides": {"kv": "Kv"}},
        {"name": "Theirs", "source": "stdlib/other.rvl",
         "provides": {"kv": "Kv"}},
        {"name": "NoSource", "provides": {"x": "X"}}],
        "manifest": {}}
    rows, foreign = CORPUS.ir_fact_rows(TENANTS, doc)
    assert [r.split("\t")[2] for r in rows] == ["Mine"]
    assert foreign == 2


def test_a_lost_isolate_entry_fails_the_kept_row_only():
    m, jr = _tenants_facts()
    # The lowering defect this row exists to catch: the realm is dropped from
    # ONE provider. The document is still disjoint, the slots are not the
    # composition's, and that is why the kept row exists beside the other.
    mutated = [r[:] for r in jr]
    victim = next(r for r in mutated if r[2] == "TenantBStore")
    victim[4] = "-"
    disjoint, kept = CORPUS._ir_reference(m, mutated)
    assert disjoint == {TENANTS: "ok"}
    assert kept == {TENANTS: "fail"}


def test_a_lost_isolate_entry_on_both_providers_fails_both_rows():
    m, jr = _tenants_facts()
    mutated = [r[:] for r in jr]
    for r in mutated:
        if r[2] in ("TenantAStore", "TenantBStore"):
            r[4] = "-"
    disjoint, kept = CORPUS._ir_reference(m, mutated)
    assert disjoint == {TENANTS: "fail"}
    assert kept == {TENANTS: "fail"}


def test_a_duplicated_slot_fails_the_kept_row_as_a_multiset():
    m, jr = _tenants_facts()
    mutated = [r[:] for r in jr] + [[*jr[0]]]
    mutated[-1][2] = "Ghost"
    _disjoint, kept = CORPUS._ir_reference(m, mutated)
    assert kept == {TENANTS: "fail"}


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

    saved = list(CORPUS._IR_UNLOWERED)
    CORPUS._IR_UNLOWERED.clear()
    try:
        assert CORPUS.ir_alignment(Ref()) == []

        class Drift(Ref):
            ir_kept = {TENANTS: "fail"}

        assert CORPUS.ir_alignment(Drift())

        class G2Drift(Ref):
            ir_disjoint = {TENANTS: "fail"}

        findings = CORPUS.ir_alignment(G2Drift())
        assert findings and "IR G2 drift" in findings[0]
    finally:
        CORPUS._IR_UNLOWERED[:] = saved


def test_a_file_the_checker_accepts_with_no_ir_row_is_a_finding():
    # The reviewer's floor: a refusal is an absence by design, an ACCEPTED file
    # that yields no `JR` row is how a lowering that stops emitting a file's
    # components would pass, and a guard that skipped it would hide it.
    class Ref:
        ir_kept = {}
        ir_disjoint = {}
        files = {}

    saved = list(CORPUS._IR_UNLOWERED)
    CORPUS._IR_UNLOWERED[:] = ["examples/a.rvl"]
    try:
        findings = CORPUS.ir_alignment(Ref())
    finally:
        CORPUS._IR_UNLOWERED[:] = saved
    assert len(findings) == 1 and "IR rows missing" in findings[0]
    assert "examples/a.rvl" in findings[0]


def test_export_fails_closed_when_lowering_emits_none_of_a_files_components(
        tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.rvl").write_text(
        (ROOT / TENANTS).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(CORPUS, "REPO", tmp_path)
    monkeypatch.setattr(CORPUS, "CORPUS_DIRS", ("corpus",))
    real = CORPUS.compile_files

    # a lowering that stops emitting the file's own components
    monkeypatch.setattr(CORPUS, "compile_files", lambda paths, *a, **k: {
        **real(paths, *a, **k), "components": []})
    CORPUS.export()
    assert CORPUS._IR_UNLOWERED == ["corpus/a.rvl"]

    # a checker REFUSAL is an absence by design, not a finding
    def refuse(paths, *a, **k):
        from revl.errors import RevlError
        raise RevlError(paths[0], 1, "refused")

    monkeypatch.setattr(CORPUS, "compile_files", refuse)
    CORPUS.export()
    assert CORPUS._IR_UNLOWERED == []

    # and an honest lowering yields rows and no finding
    monkeypatch.setattr(CORPUS, "compile_files", real)
    rows, _facts_, _census = CORPUS.export()
    assert CORPUS._IR_UNLOWERED == []
    assert sum(1 for r in rows if r.startswith("JR\t")) == 4


def test_ir_coverage_fails_when_no_realm_or_template_was_exercised():
    saved = dict(CORPUS._IR_ROWS)
    try:
        CORPUS._IR_ROWS.clear()
        assert CORPUS.ir_coverage()
        CORPUS._IR_ROWS.update({
            "a.rvl": {"components": 1, "templates": 0,
                      "slots": [("kv", "")], "realms": 0,
                      "template_collision": False},
        })
        findings = CORPUS.ir_coverage()
        assert any("realm-pinned" in f for f in findings)
        assert any("templates` list is never load-bearing" in f
                   for f in findings)
    finally:
        CORPUS._IR_ROWS.clear()
        CORPUS._IR_ROWS.update(saved)


def test_the_lean_side_decides_the_ir_rows():
    """`IRD` is `decide (RevL.G2.IRDisjoint d)` and `IRS` is `decide` of the
    permutation `source_linkOK_ir_disjoint` takes as `hk`; both have a bridge
    theorem registered with the axiom gate."""
    oracle = (ROOT / "formal" / "harness" / "Oracle.lean").read_text(
        encoding="utf-8")
    assert "def irdisjointB" in oracle and "theorem irdisjointB_iff" in oracle
    assert "def irKeptB" in oracle and "theorem irKeptB_iff" in oracle
    assert "d.slots.Perm (src.flatMap slots)" in oracle
    assert "sameSlots" not in oracle, "set equality is not the claim"
    assert "#print axioms RevLOracle.irdisjointB_iff" in oracle
    assert "#print axioms RevLOracle.irKeptB_iff" in oracle
    gate = (ROOT / "formal" / "scripts" / "run_gate.sh").read_text(
        encoding="utf-8")
    assert "RevLOracle.irdisjointB_iff" in gate
    assert "RevLOracle.irKeptB_iff" in gate
    assert "IRD\\t" in oracle and "IRS\\t" in oracle
    assert "templates := (jc.filter (·.isTemplate)).map (·.comp)" in oracle


def test_the_ir_image_theorems_are_registered():
    names = ("source_linkOK_ir_disjoint", "ir_disjoint_iff_source_disjoint",
             "tenants_ir_kept", "tenants_ir_disjoint",
             "isolate_entry_is_load_bearing", "lost_realm_is_caught_by_kept",
             "template_exclusion_is_load_bearing")
    check = (ROOT / "formal" / "CheckAxioms.lean").read_text(encoding="utf-8")
    gate = (ROOT / "formal" / "scripts" / "run_gate.sh").read_text(
        encoding="utf-8")
    registry = (ROOT / "formal" / "scripts" / "nonvacuity.tsv").read_text(
        encoding="utf-8")
    status = STATUS.read_text(encoding="utf-8")
    for name in names:
        assert f"#print axioms RevL.G2.{name}" in check, name
        assert f"RevL.G2.{name} " in gate, name
        assert f"RevL.G2.{name}\t" in registry, name
        assert f"`RevL.G2.{name}`" in status, name
    # the restatement the review found is gone: `ofIR` set `requires := []`, so
    # `LinkOK (cs.map ofIR)` was equivalent to its own conclusion
    lean = (ROOT / "formal" / "RevL" / "Theorems"
            / "G2_ProvisionDisjoint.lean").read_text(encoding="utf-8")
    assert "ofIR" not in lean and "linkOK_ir_disjoint" not in lean.replace(
        "source_linkOK_ir_disjoint", "")
    assert "hk : d.slots.Perm (src.flatMap slots)" in lean
