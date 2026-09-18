"""The A9 row of the formal differential harness (issue 1167).

A9 is the checker rule that a `provide k { … }` block's key is named in the
component's `provides` clause (`lower._lower_provide`: "`k` is not declared
in the `provides` clause of C (A9)"). `formal/RevL/Theorems/
A9_ProvideKeyDeclared.lean` models the installed blocks beside the L0
`LComponent` and proves that under A9 every block is a `(key, realm)` slot of
the G2/G3 universe; `formal/harness/diff_corpus.py` exports the blocks as
`PB` fact rows, recomputes the verdict from the TSV, and diffs it against the
Lean oracle's `A9` row.

`make formal` needs a Lean toolchain, so this module pins the PYTHON half in
the plain `pytest tests/` job (the #1141 / #1164 pattern): the fact the row
reads, the reference's verdict on the refused fixture and on an admitted
provider, the `A9` arm of `checker_alignment` in both directions, and the
coverage ratchet. Nothing here runs Lean.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

#: The refused shape: `provides skin1: Skin` with `provide skin { … }`.
FIXTURE = "examples/rejections/a9_provide_key_not_declared.rvl"

#: An admitted provider with a block under a declared key.
ADMITTED = ("examples/tenants.rvl", "TenantAStore")

LEAN = ROOT / "formal" / "RevL" / "Theorems" / "A9_ProvideKeyDeclared.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "formal_diff_corpus", ROOT / "formal" / "harness" / "diff_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return [r.split("\t") for r in rows]


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(["\t".join(r) for r in tsv])


def _rows(tsv, kind, rel=None):
    return [r for r in tsv
            if r and r[0] == kind and (rel is None or r[1] == rel)]


# ------------------------------------------------------------ the premise

def test_the_reference_refuses_the_fixture_under_a9():
    """If this stops refusing, the rest of the module measures the wrong
    thing and should be read again, not deleted."""
    from revl import compile_files
    from revl.diagnostics import classify
    from revl.errors import RevlError

    with pytest.raises(RevlError) as excinfo:
        compile_files([str(ROOT / FIXTURE)])
    assert classify(excinfo.value)["code"] == "A9"
    assert "`skin` is not declared in the `provides` clause of S" in str(
        excinfo.value)


# --------------------------------------------------- the fact the row reads

def test_the_pb_row_reads_the_block_and_the_c_row_reads_the_clause(tsv):
    """The two facts come off two AST nodes. On the fixture they DISAGREE,
    which is the whole content of A9: a row computed from `C` alone could
    never see the refusal."""
    assert [(r[2], r[3]) for r in _rows(tsv, "PB", FIXTURE)] == [("S", "skin")]
    assert [(r[2], r[3]) for r in _rows(tsv, "C", FIXTURE)] == [("S", "skin1")]
    mrow = _rows(tsv, "M", FIXTURE)
    assert len(mrow) == 1 and mrow[0][4] == "skin1"


def test_a_double_install_would_be_a_repeated_row(tsv):
    """`PB` is one row per block in body order, not a set: the corpus has no
    double install today, so every component's PB keys are distinct, and a
    collapsed export could not tell a second block from the first."""
    seen: dict[tuple[str, str], list[str]] = {}
    for r in _rows(tsv, "PB"):
        seen.setdefault((r[1], r[2]), []).append(r[3])
    assert seen, "no provide block exported at all"
    assert all(len(v) == len(set(v)) for v in seen.values())


# -------------------------------------------------- the reference verdict

def test_the_reference_fails_the_fixture_and_admits_a_provider(verdicts):
    assert verdicts.a9[(FIXTURE, "S")] == "fail"
    assert verdicts.a9[ADMITTED] == "ok"


def test_a_component_with_no_block_gets_no_row(tsv, verdicts):
    """A block-less component would agree vacuously, so it has no A9 row;
    every row is a component that installs something."""
    installing = {(r[1], r[2]) for r in _rows(tsv, "PB")}
    assert set(verdicts.a9) == installing


def test_the_only_reference_fail_is_the_fixture(verdicts):
    """The corpus carries exactly one refused shape. A second `fail` here is
    either a new fixture (add it to this list) or a reference regression."""
    assert [k for k, v in verdicts.a9.items() if v == "fail"] == [(FIXTURE, "S")]


def test_the_reference_is_a_membership_between_the_two_facts(harness, tsv):
    """Recompute the verdict from the raw rows with no harness code at all,
    so the reference cannot drift into reading the clause twice."""
    provides = {(r[1], r[2]): r[4].split(",") for r in _rows(tsv, "M")}
    blocks: dict[tuple[str, str], list[str]] = {}
    for r in _rows(tsv, "PB"):
        blocks.setdefault((r[1], r[2]), []).append(r[3])
    want = {k: ("ok" if all(b in provides[k] for b in bs) else "fail")
            for k, bs in blocks.items()}
    got = harness.reference_from_tsv(["\t".join(r) for r in tsv]).a9
    assert got == want


# ---------------------------------------------------- the alignment arm

def _align(harness, verdicts, rel):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fatal = harness.checker_alignment({rel: {}}, [], verdicts)
    counts = dict(re.findall(r"^  ([a-zA-Z0-9-]+)\s+(\d+)(?:\s+FATAL)?$",
                             buf.getvalue(), re.MULTILINE))
    return counts, fatal


def test_missed_a9_is_fatal(harness):
    assert "missed-A9" in harness.FATAL_BUCKETS


def test_the_fixture_lands_in_agree_a9(harness, verdicts):
    counts, fatal = _align(harness, verdicts, FIXTURE)
    assert counts == {"agree-A9": "1"}
    assert fatal == []


def test_a_blind_row_lands_in_missed_a9(harness, verdicts):
    """Make the row blind (say `ok` on the fixture) and the arm must fail the
    gate: the checker refusing where the model sees nothing is the direction
    a differential oracle cannot afford to report as agreement."""
    blind = verdicts._replace(a9={**verdicts.a9, (FIXTURE, "S"): "ok"})
    counts, fatal = _align(harness, blind, FIXTURE)
    assert counts == {"missed-A9": "1"}
    assert fatal == [f"missed-A9: {FIXTURE}"]


def test_an_a9_fail_is_not_formal_clean(harness, verdicts):
    """`formal_clean` reads the A9 row: an admitted file whose row said
    `fail` would be `formal-strict`, not `agree-accept`."""
    rel = ADMITTED[0]
    counts, _ = _align(harness, verdicts, rel)
    assert counts == {"agree-accept": "1"}
    strict = verdicts._replace(a9={**verdicts.a9, ADMITTED: "fail"})
    counts, fatal = _align(harness, strict, rel)
    assert counts == {"formal-strict": "1"}
    assert fatal == []


# ------------------------------------------------------- the ratchet

def test_the_coverage_ratchet_is_satisfied(harness, verdicts):
    buf = io.StringIO()
    with redirect_stdout(buf):
        findings = harness.a9_coverage()
    assert findings == []
    assert FIXTURE in buf.getvalue()


def test_the_coverage_ratchet_bites_without_the_refused_shape(harness, verdicts):
    kept = dict(harness._A9_ROWS)
    try:
        harness._A9_ROWS.clear()
        harness._A9_ROWS.update({k: v for k, v in kept.items() if v[0]})
        findings = harness.a9_coverage()
        assert len(findings) == 1 and "never declared" in findings[0]
        harness._A9_ROWS.clear()
        findings = harness.a9_coverage()
        assert len(findings) == 2
    finally:
        harness._A9_ROWS.clear()
        harness._A9_ROWS.update(kept)


# --------------------------------------------- the Lean side, as text

def test_the_oracle_decides_the_row_with_the_model_a9b():
    """The verdict row is the L2 file's own `a9B`, bridged by `a9RowB_iff`,
    and that bridge is under the axioms gate."""
    src = ORACLE.read_text()
    assert re.search(r"def a9RowB .*:=\s*\n\s*RevL\.A9\.a9B ⟨c, blocks⟩", src)
    assert "#print axioms RevLOracle.a9RowB_iff" in src
    assert "RevLOracle.a9RowB_iff" in GATE.read_text()


def test_the_l2_file_states_only_what_the_checker_enforces():
    """Two predicates, matching `_lower_provide`'s two refusals, and a
    written refusal to state the converse the checker does not enforce."""
    src = LEAN.read_text()
    assert re.search(r"def A9OK .*:= ∀ k ∈ i\.blocks, k ∈ i\.comp\.provides", src)
    assert re.search(r"def NoDoubleInstall .*:= List\.Nodup i\.blocks", src)
    assert "a declared key with no block — is NOT enforced" in src
    assert "sorry" not in src
