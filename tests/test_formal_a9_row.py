"""The A9 row of the formal differential harness (issues #1167 and #1172).

A9 is the checker rule that a `provide k { … }` block's key is named in the
component's `provides` clause, in BOTH directions:

  * `lower._lower_provide`: "`k` is not declared in the `provides` clause of C
    (A9)" (#1167);
  * `lower._lower_component`, after the body walk: "`k` is declared in the
    `provides` clause of C but no `provide k { … }` block installs it (A9)"
    (#1172, PR #1184), with the multi-realm bind `isolate k in realms(...)`
    (the IR's `routes`) as the one form that installs a key without a block.

`formal/RevL/Theorems/A9_ProvideKeyDeclared.lean` models the installed blocks
and routed keys beside the L0 `LComponent` and proves that under A9 every
block is a `(key, realm)` slot of the G2/G3 universe; `formal/harness/
diff_corpus.py` exports the blocks as `PB` fact rows and the routes as `PR`
fact rows, recomputes the verdict from the TSV, and diffs it against the Lean
oracle's `A9` row.

`make formal` needs a Lean toolchain, so this module pins the PYTHON half in
the plain `pytest tests/` job (the #1141 / #1164 pattern): the facts the row
reads, the reference's verdict on both refused fixtures, on an admitted
provider and on the routed shape, the `A9` arm of `checker_alignment` in both
directions, and the coverage ratchet. Nothing here runs Lean.
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

#: Direction 1's refused shape: `provides skin1: Skin` with `provide skin { … }`.
FIXTURE = "examples/rejections/a9_provide_key_not_declared.rvl"

#: Direction 2's refused shape: `provides skin: Skin` and no block at all.
CONVERSE_FIXTURE = "examples/rejections/a9_provides_without_block.rvl"

#: The routes exemption, exercised: stdlib/router.rvl's shape in the corpus.
ROUTED = "tests/formal_corpus/a9_routes_installs_key.rvl"

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


def _compile(rel: str):
    from revl import compile_files
    from revl.diagnostics import classify
    from revl.errors import RevlError

    try:
        compile_files([str(ROOT / rel)])
    except RevlError as e:
        return classify(e)["code"], str(e)
    return "accept", ""


# ------------------------------------------------------------ the premise

def test_the_reference_refuses_the_fixture_under_a9():
    """If this stops refusing, the rest of the module measures the wrong
    thing and should be read again, not deleted."""
    code, msg = _compile(FIXTURE)
    assert code == "A9"
    assert "`skin` is not declared in the `provides` clause of S" in msg


def test_the_reference_refuses_the_converse_fixture_under_a9():
    code, msg = _compile(CONVERSE_FIXTURE)
    assert code == "A9"
    assert ("`skin` is declared in the `provides` clause of S but no "
            "`provide skin { … }` block installs it") in msg


def test_the_reference_admits_the_routed_shape():
    """The exemption, on the checker: `RoundRobin` declares `provides worker`,
    installs no block, and is admitted because the route installs the key."""
    assert _compile(ROUTED) == ("accept", "")


# --------------------------------------------------- the facts the row reads

def test_the_pb_row_reads_the_block_and_the_c_row_reads_the_clause(tsv):
    """The two facts come off two AST nodes. On the fixture they DISAGREE,
    which is the whole content of A9: a row computed from `C` alone could
    never see the refusal."""
    assert [(r[2], r[3]) for r in _rows(tsv, "PB", FIXTURE)] == [("S", "skin")]
    assert [(r[2], r[3]) for r in _rows(tsv, "C", FIXTURE)] == [("S", "skin1")]
    mrow = _rows(tsv, "M", FIXTURE)
    assert len(mrow) == 1 and mrow[0][4] == "skin1"


def test_the_converse_fixture_declares_and_installs_nothing(tsv):
    """`S` has a clause and no block; `User` has both. The row must see `S`
    even though `S` contributes no `PB` row at all."""
    assert [(r[2], r[3]) for r in _rows(tsv, "C", CONVERSE_FIXTURE)] == [
        ("S", "skin"), ("User", "out")]
    assert [(r[2], r[3]) for r in _rows(tsv, "PB", CONVERSE_FIXTURE)] == [
        ("User", "out")]
    assert _rows(tsv, "PR", CONVERSE_FIXTURE) == []


def test_the_pr_row_carries_the_route_as_data(tsv):
    """The exemption is a fact off the `RouteStmt`, not a heuristic: exactly
    `RoundRobin`'s `worker`, and the backends carry blocks instead."""
    assert [(r[2], r[3]) for r in _rows(tsv, "PR", ROUTED)] == [
        ("RoundRobin", "worker")]
    assert sorted((r[2], r[3]) for r in _rows(tsv, "PB", ROUTED)) == [
        ("PoolWorker1", "worker"), ("PoolWorker2", "worker"),
        ("PoolWorker3", "worker")]
    assert [r for r in _rows(tsv, "PR") if r[1] != ROUTED] == []


def test_the_m_row_keeps_the_routed_requirement(tsv):
    """`M` stays the faithful manifest: the routed requirement is still on it,
    and it is the V-row model that elides it (the next test)."""
    mrow = [r for r in _rows(tsv, "M", ROUTED) if r[2] == "RoundRobin"]
    assert len(mrow) == 1 and mrow[0][3] == "worker" and mrow[0][4] == "worker"


def test_the_v_row_elides_the_routed_requirement(verdicts):
    """The linker resolves a routed key per leg, never through the single-realm
    table; spelled into the shared realm it would read as `RoundRobin`
    requiring its own provision (a phantom G3 self-provision). The file links
    on both sides, as revl admits it."""
    assert verdicts.files[ROUTED] == ("ok", "ok", "ok")


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

def test_the_reference_fails_both_fixtures_and_admits_providers(verdicts):
    assert verdicts.a9[(FIXTURE, "S")] == "fail"
    assert verdicts.a9[(CONVERSE_FIXTURE, "S")] == "fail"
    assert verdicts.a9[(CONVERSE_FIXTURE, "User")] == "ok"
    assert verdicts.a9[ADMITTED] == "ok"
    assert verdicts.a9[(ROUTED, "RoundRobin")] == "ok"
    assert verdicts.a9[(ROUTED, "PoolWorker1")] == "ok"


def test_a_component_that_neither_declares_nor_installs_gets_no_row(tsv, verdicts):
    """A row is a component with a clause or a block; one with neither would
    agree vacuously and gets none. Every component that declares a key is
    under the row, which is what direction 2 needs."""
    declaring = {(r[1], r[2]) for r in _rows(tsv, "M") if r[4]}
    installing = {(r[1], r[2]) for r in _rows(tsv, "PB")}
    assert set(verdicts.a9) == declaring | installing
    assert (CONVERSE_FIXTURE, "S") in declaring - installing


def test_the_only_reference_fails_are_the_two_fixtures(verdicts):
    """The corpus carries exactly the two refused shapes. A third `fail`
    here is either a new fixture (add it to this list) or a reference
    regression."""
    assert sorted(k for k, v in verdicts.a9.items() if v == "fail") == sorted([
        (CONVERSE_FIXTURE, "S"), (FIXTURE, "S")])


def test_the_reference_is_a_membership_between_the_three_facts(harness, tsv):
    """Recompute the verdict from the raw rows with no harness code at all,
    so the reference cannot drift into reading the clause twice."""
    provides = {(r[1], r[2]): [k for k in r[4].split(",") if k]
                for r in _rows(tsv, "M")}
    blocks: dict[tuple[str, str], list[str]] = {}
    for r in _rows(tsv, "PB"):
        blocks.setdefault((r[1], r[2]), []).append(r[3])
    routed: dict[tuple[str, str], list[str]] = {}
    for r in _rows(tsv, "PR"):
        routed.setdefault((r[1], r[2]), []).append(r[3])
    want = {}
    for k in set(provides) | set(blocks):
        decl, bs, rs = provides.get(k, []), blocks.get(k, []), routed.get(k, [])
        if not decl and not bs:
            continue
        ok = all(b in decl for b in bs) and all(d in bs or d in rs for d in decl)
        want[k] = "ok" if ok else "fail"
    got = harness.reference_from_tsv(["\t".join(r) for r in tsv]).a9
    assert got == want


def test_the_route_fact_is_load_bearing(harness, tsv):
    """Drop the `PR` row and `RoundRobin` flips to `fail`: the exemption is
    read off the fact, not inferred from the clause or the requirement."""
    without = ["\t".join(r) for r in tsv if not (r[0] == "PR" and r[1] == ROUTED)]
    try:
        assert harness.reference_from_tsv(without).a9[(ROUTED, "RoundRobin")] == "fail"
    finally:
        # `reference_from_tsv` refills the module-level ratchet evidence;
        # put the full corpus's back for the ratchet tests below.
        harness.reference_from_tsv(["\t".join(r) for r in tsv])


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


def test_both_fixtures_land_in_agree_a9(harness, verdicts):
    for rel in (FIXTURE, CONVERSE_FIXTURE):
        counts, fatal = _align(harness, verdicts, rel)
        assert counts == {"agree-A9": "1"}, rel
        assert fatal == []


def test_the_routed_shape_lands_in_agree_accept(harness, verdicts):
    counts, fatal = _align(harness, verdicts, ROUTED)
    assert counts == {"agree-accept": "1"}
    assert fatal == []


def test_a_blind_row_lands_in_missed_a9(harness, verdicts):
    """Make the row blind (say `ok` on a fixture) and the arm must fail the
    gate: the checker refusing where the model sees nothing is the direction
    a differential oracle cannot afford to report as agreement. Both
    directions, because a decider blind to direction 2 alone is exactly what
    #1184 found on CI."""
    for rel in (FIXTURE, CONVERSE_FIXTURE):
        blind = verdicts._replace(a9={**verdicts.a9, (rel, "S"): "ok"})
        counts, fatal = _align(harness, blind, rel)
        assert counts == {"missed-A9": "1"}, rel
        assert fatal == [f"missed-A9: {rel}"]


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
    for rel in (FIXTURE, CONVERSE_FIXTURE, ROUTED):
        assert rel in buf.getvalue()


def test_the_coverage_ratchet_bites_without_each_witness(harness, verdicts):
    kept = dict(harness._A9_ROWS)

    def only(pred):
        harness._A9_ROWS.clear()
        harness._A9_ROWS.update({k: v for k, v in kept.items() if pred(v)})
        return harness.a9_coverage()

    try:
        found = only(lambda v: v[0])  # drop direction 1's refusal
        assert len(found) == 1 and "never declared" in found[0]
        # drop direction 2's refusal alone: the first fixture fails BOTH
        # directions and stays, so only the converse witness goes missing
        found = only(lambda v: v[1] or not v[0])
        assert len(found) == 1 and "nothing installs" in found[0]
        found = only(lambda v: not v[3])  # drop the routed witness
        assert len(found) == 1 and "routed key" in found[0]
        harness._A9_ROWS.clear()
        assert len(harness.a9_coverage()) == 4
    finally:
        harness._A9_ROWS.clear()
        harness._A9_ROWS.update(kept)


# --------------------------------------------- the Lean side, as text

def test_the_oracle_decides_the_row_with_the_model_a9b():
    """The verdict row is the L2 file's own `a9B` over blocks AND routes,
    bridged by `a9RowB_iff`, and that bridge is under the axioms gate."""
    src = ORACLE.read_text()
    assert re.search(r"def a9RowB .*:=\s*\n\s*RevL\.A9\.a9B ⟨c, blocks, routed⟩", src)
    assert "#print axioms RevLOracle.a9RowB_iff" in src
    assert "RevLOracle.a9RowB_iff" in GATE.read_text()


def test_the_l2_file_states_both_directions_and_the_caveats():
    """Two directions matching `_lower_provide` and `_lower_component`'s two
    refusals, the double install beside them, and the two things the model
    does not claim written down."""
    src = LEAN.read_text()
    assert re.search(r"def BlocksDeclared .*:= ∀ k ∈ i\.blocks, k ∈ i\.comp\.provides", src)
    assert re.search(r"def DeclaredInstalled .*:=\s*\n\s*∀ k ∈ i\.comp\.provides, "
                     r"k ∈ i\.blocks ∨ k ∈ i\.routed", src)
    assert re.search(r"def A9OK .*:= BlocksDeclared i ∧ DeclaredInstalled i", src)
    assert re.search(r"def NoDoubleInstall .*:= List\.Nodup i\.blocks", src)
    assert "the checker SKIPS the converse for a body that recovered" in src
    assert "the route's realm legs" in src
    assert "issue #1172, PR #1184" in src
    assert "sorry" not in src
