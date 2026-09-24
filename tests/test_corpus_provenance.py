"""The corpus-provenance gate: it fires, it fires in the fail-CLOSED direction,
and it does not move the census's verdicts (roadmap item 542, issue #1221).

This repository has measured eleven checks that ran on every PR and could not
fail, so a gate that ships with no demonstration of its own firing condition is
worth nothing. Every claim below is held by an assertion:

  * THE FLOOR FIRES. `test_the_gate_fires_on_a_tree_that_crosses_the_floor`
    builds a corpus over the declared floor and one under it, from the same
    document set, and asserts the two verdicts differ.
  * THE CONTROL IS FLAT. In the same pair of runs a second corpus is identical
    on both sides and its verdict does not move, so the difference belongs to
    the document whose generation changed and not to the harness.
  * THE DEFAULT IS CLOSED. An undeclared document is model-authored at EVERY
    threshold, is not generation 0, and is an error of its own.
  * THE LIVE GUARD. `check()` over the real tree is green today, which makes
    the declaration requirement fire on the next PR that adds a corpus
    document without a manifest line -- the arm that runs on real inputs
    rather than on a constructed one.
  * REPORTING ONLY. The census's verdict path never reads the manifest.
  * TWO AXES, NOT ONE. Generation answers "did the loop write this"; the
    `human_authored` list answers "did a person type this". Issue #1397
    measured that the first was being read as the second, so the tests below
    hold them apart: a document can be generation 0 and still not human, and
    the authorship axis carries no floor.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location(
        "corpus_provenance", ROOT / "tools" / "corpus_provenance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _census():
    spec = importlib.util.spec_from_file_location(
        "gate_reference_census", ROOT / "tools" / "gate_reference_census.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest(generations, floors, current=0, human=()):
    return _tool().Provenance({"current_generation": current,
                               "floors": floors,
                               "human_authored": list(human),
                               "generations": generations})


# ------------------------------------------------- the fail-closed direction

def test_an_undeclared_document_is_not_hand_written():
    """The whole measurement rests on this. A provenance that defaults to
    generation 0 when unknown would make dropping an undeclared document into a
    globbed corpus RAISE the measured independence."""
    tool = _tool()
    prov = _manifest({"0": ["declared.rvl"]}, {})
    assert prov.generation("declared.rvl") == 0
    assert prov.generation("dropped_in.rvl") == tool.UNDECLARED
    assert prov.generation("dropped_in.rvl") != 0


def test_an_undeclared_document_is_model_authored_at_every_threshold():
    prov = _manifest({"0": ["hand.rvl"]}, {})
    for since in (1, 2, 7, 10_000):
        assert prov.is_model_authored("dropped_in.rvl", since=since)
        assert not prov.is_model_authored("hand.rvl", since=since)


def test_an_undeclared_document_lowers_the_measured_independence():
    """The direction that makes forgetting a manifest line safe to forget
    NOTHING: the tree looks worse, not better."""
    tool = _tool()
    prov = _manifest({"0": ["a.rvl", "b.rvl"]}, {"c": 100})
    declared = ["a.rvl", "b.rvl"]
    with_unknown = declared + ["c.rvl"]
    assert tool.independent_permille(*_split(prov, declared)) == 1000
    assert tool.independent_permille(*_split(prov, with_unknown)) < 1000


def _split(prov, ids):
    model, _, _ = prov.split(ids)
    return len(model), len(ids)


def test_write_refuses_to_guess_a_generation():
    """`--write` with no `--generation` would default to the value a caller
    wants, which is 0, which is the fail-open one."""
    tool = _tool()
    with pytest.raises(SystemExit):
        tool.main(["--write"])


def test_since_zero_is_refused():
    tool = _tool()
    with pytest.raises(SystemExit):
        tool.main(["--since", "0"])


# ------------------------------------------------------- the gate, and that
# ------------------------------------------------------- it actually fires

FLOOR = 80
DOCUMENTS = [f"corpus/doc{i:02d}.rvl" for i in range(20)]
CONTROL = [f"control/doc{i:02d}.rvl" for i in range(20)]


def _tree(model_authored):
    """A manifest over `DOCUMENTS` + `CONTROL` with `model_authored` of the
    first at generation 1, everything else at generation 0. The CONTROL corpus
    is generation 0 in every arm, and is the control."""
    gen1 = DOCUMENTS[:model_authored]
    gen0 = [d for d in DOCUMENTS if d not in gen1] + CONTROL
    return _manifest({"0": gen0, "1": gen1},
                     {"corpus": FLOOR, "control": FLOOR})


CORPORA = {"corpus": DOCUMENTS, "control": CONTROL}


def test_the_gate_fires_on_a_tree_that_crosses_the_floor():
    """4 of 20 model-authored is exactly 80% independent and passes; 5 of 20 is
    75% and fails. The floor is a real edge, not a direction."""
    tool = _tool()
    under = tool.check(CORPORA, _tree(4))
    over = tool.check(CORPORA, _tree(5))

    assert under == [], f"80% independent must pass an 80% floor, got {under}"
    assert len(over) == 1, over
    assert over[0].startswith("BELOW FLOOR corpus:"), over
    assert "75.0% independent" in over[0], over
    assert "floor is 80%" in over[0], over


def test_the_control_corpus_does_not_move_between_those_two_runs():
    """The same 20 generation-0 documents, in both arms, under the same floor.
    If `control` moved, the difference in the run above would not belong to the
    document whose generation changed."""
    tool = _tool()
    control_under = [p for p in tool.check(CORPORA, _tree(4)) if "control" in p]
    control_over = [p for p in tool.check(CORPORA, _tree(5)) if "control" in p]
    assert control_under == control_over == []


def test_the_floor_is_evaluated_per_corpus_not_in_aggregate():
    """A tier whose corpus has gone model-authored must red even when the
    aggregate over every corpus is comfortably above the floor."""
    tool = _tool()
    prov = _manifest({"0": CONTROL, "1": DOCUMENTS},
                     {"corpus": FLOOR, "control": FLOOR})
    problems = tool.check(CORPORA, prov)
    assert [p for p in problems if p.startswith("BELOW FLOOR corpus:")]
    assert not [p for p in problems if p.startswith("BELOW FLOOR control:")]


def test_an_undeclared_document_reds_the_gate_by_name():
    tool = _tool()
    prov = _manifest({"0": DOCUMENTS[1:] + CONTROL},
                     {"corpus": 0, "control": 0})
    problems = tool.check(CORPORA, prov)
    assert any(p.startswith(f"UNDECLARED provenance in corpus: {DOCUMENTS[0]}")
               for p in problems), problems


def test_a_manifest_entry_that_outlived_its_document_reds_the_gate():
    """Same arm as the census baseline's `no longer diverges`: an allowance
    that outlives what it described is an allowance nobody rereads."""
    tool = _tool()
    prov = _manifest({"0": DOCUMENTS + CONTROL + ["corpus/deleted.rvl"]},
                     {"corpus": 0, "control": 0})
    problems = tool.check(CORPORA, prov)
    assert any(p.startswith("STALE provenance entry: corpus/deleted.rvl")
               for p in problems), problems


def test_a_corpus_with_no_declared_floor_reds_the_gate():
    """A scoring corpus added with no floor is a corpus nothing holds. It is
    named rather than skipped."""
    tool = _tool()
    prov = _manifest({"0": DOCUMENTS + CONTROL}, {"corpus": FLOOR})
    problems = tool.check(CORPORA, prov)
    assert any(p.startswith("NO FLOOR declared for scoring corpus control")
               for p in problems), problems


def test_an_empty_scoring_corpus_crosses_every_floor():
    """A corpus that vanished is not a corpus that passed: `0 of 0` is the one
    reading under which a ratchet over fractions goes quiet."""
    tool = _tool()
    assert tool.crosses_floor(0, 0, 1)
    assert tool.crosses_floor(0, 0, 100)
    assert not tool.crosses_floor(0, 0, 0)
    prov = _manifest({"0": CONTROL}, {"corpus": FLOOR, "control": FLOOR})
    problems = tool.check({"corpus": [], "control": CONTROL}, prov)
    assert any(p.startswith("BELOW FLOOR corpus:") for p in problems), problems


def test_the_floor_comparison_uses_no_floating_point():
    """3 model-authored of 7 is 57.142...% and a float threshold is where an
    edge case starts depending on the platform. The comparison is integer."""
    tool = _tool()
    assert tool.crosses_floor(3, 7, 58)
    assert not tool.crosses_floor(3, 7, 57)
    assert tool.independent_permille(3, 7) == 571


# ------------------------------------------------------- the census coupling

def test_the_enumeration_uses_the_census_own_case_ids():
    """The per-bucket table joins on the census's ids, so an enumeration that
    named its documents differently would report a fraction over the wrong
    population. An earlier version read the oracle lists with `ast` and missed
    the 16 programs the list appends in a loop."""
    tool = _tool()
    census = _census()
    import test_selfhost_lower as oracle

    want = [cid for cid, _ in census.load_corpus(oracle)]
    assert tool.enumerate_corpora()["census"] == want
    assert len(want) > 800


def test_the_census_verdict_path_never_reads_the_manifest():
    """The provenance table is reporting only. `bucket`, `compare`, `report`
    and the `--record` payload must not be able to change when the manifest
    does, or a provenance edit could move a gate/reference verdict.

    Held structurally as well as behaviourally: the manifest is reachable from
    exactly one function in the census, `provenance_report`, which only `main`
    calls and only to print."""
    census = _census()
    buckets = {"agree-admit": ["a.rvl"], "false-admit/G1": ["b.rvl"]}
    baseline = {"buckets": {"false-admit/G1": ["b.rvl"]}}
    assert census.compare(buckets, baseline) == []
    assert census.bucket(("", ""), ("no_objection", "")) == "agree-admit"
    assert "provenance" not in census.report(buckets)

    import ast
    tree = ast.parse((ROOT / "tools" / "gate_reference_census.py").read_text())
    callers = {
        node.name: {c.func.id for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert {n for n, c in callers.items() if "_provenance" in c} == {
        "provenance_report"}
    assert {n for n, c in callers.items() if "provenance_report" in c} == {"main"}
    for name in ("bucket", "run", "report", "compare", "load_corpus"):
        assert "provenance_report" not in callers[name]
        assert "_provenance" not in callers[name]


def test_the_committed_manifest_covers_the_real_scoring_corpora():
    """The live guard. Green today, which is what makes it fire on the next PR
    that adds a corpus document and no manifest line."""
    tool = _tool()
    problems = tool.check(tool.enumerate_corpora(), tool.Provenance.load())
    assert problems == [], "\n".join(problems)


def test_every_scoring_corpus_is_non_empty_and_has_a_floor():
    tool = _tool()
    prov = tool.Provenance.load()
    corpora = tool.enumerate_corpora()
    assert set(corpora) >= {"census", "selfhost_oracle", "emit_py_corpus"}
    for name, ids in corpora.items():
        assert ids, f"{name} is empty"
        assert name in prov.floors, f"{name} has no declared floor"


def test_rewriting_the_manifest_reproduces_it_byte_for_byte(tmp_path):
    """Names only, no counts and no fractions, so a regeneration is a no-op
    diff. This is PR #1203's discipline: a derived number in a recorded
    artifact rots against what it was derived from."""
    tool = _tool()
    out = tmp_path / "corpus_provenance.json"
    tool.write(tool.enumerate_corpora(), tool.Provenance.load(), 0, path=out)
    assert out.read_bytes() == tool.MANIFEST.read_bytes()


def test_the_manifest_records_no_counts_or_fractions():
    data = json.loads(_tool().MANIFEST.read_text(encoding="utf-8"))
    assert set(data) == {"note", "current_generation", "floors",
                         "human_authored", "generations"}
    assert data["human_authored"] == sorted(data["human_authored"])
    for names in data["generations"].values():
        assert names == sorted(names)
        assert all(isinstance(name, str) for name in names)


def test_the_bucket_report_splits_a_mixed_population():
    """The table the census prints, on a population that is not all one thing:
    a 100%-independent line for every bucket would look the same whether the
    split worked or the manifest was empty."""
    tool = _tool()
    prov = _manifest({"0": ["a.rvl", "b.rvl"], "3": ["c.rvl"]}, {})
    text = tool.bucket_report({"agree-admit": ["a.rvl", "b.rvl", "c.rvl"],
                               "false-admit/G1": ["c.rvl"]}, prov, since=1)
    assert "1 / 3" in text and "66.6% independent  agree-admit" in text
    assert "1 / 1" in text and "0.0% independent  false-admit/G1" in text
    assert "0 undeclared" in text

    later = tool.bucket_report({"agree-admit": ["a.rvl", "b.rvl", "c.rvl"]},
                               prov, since=4)
    assert "0 / 3" in later, later


# ------------------------------------------------------ the authorship axis

def test_generation_zero_does_not_make_a_document_human_authored():
    """Issue #1397. The two axes are orthogonal, and the fail-closed default
    on the second one is the same as on the first: not named is not human.
    Reading generation 0 as "a person typed it" is the conflation that made
    the published figure mean something other than what it said."""
    prov = _manifest({"0": ["pre_loop.rvl", "typed.rvl"]}, {},
                     human=["typed.rvl"])
    assert prov.generation("pre_loop.rvl") == 0
    assert not prov.is_model_authored("pre_loop.rvl")
    assert not prov.is_human_authored("pre_loop.rvl")
    assert prov.is_human_authored("typed.rvl")


def test_an_undeclared_document_is_not_human_authored_either():
    prov = _manifest({"0": ["a.rvl"]}, {}, human=["a.rvl"])
    assert not prov.is_human_authored("dropped_in.rvl")


def test_the_authorship_split_is_reported_and_carries_no_floor():
    """A corpus at 0% human must not red the gate. The floors gate the loop
    axis; a floor on the authorship axis would be a floor at zero."""
    tool = _tool()
    prov = _manifest({"0": ["a.rvl", "b.rvl"]}, {"c": 80}, human=[])
    assert tool.check({"c": ["a.rvl", "b.rvl"]}, prov) == []
    text = tool.authorship_report({"c": ["a.rvl", "b.rvl"]}, prov)
    assert "0      2     0.0%" in text, text


def test_the_authorship_split_moves_with_the_declaration():
    tool = _tool()
    ids = ["a.rvl", "b.rvl", "c.rvl", "d.rvl"]
    prov = _manifest({"0": ids}, {}, human=["a.rvl"])
    model, human = prov.human_split(ids)
    assert human == ["a.rvl"] and len(model) == 3
    assert "25.0%" in tool.authorship_report({"c": ids}, prov)


def test_a_human_authored_entry_that_outlived_its_document_reds_the_gate():
    """Same shape as the stale-generation arm: an entry that no longer names
    anything is an entry nobody rereads."""
    tool = _tool()
    prov = _manifest({"0": ["a.rvl"]}, {"c": 0}, human=["gone.rvl"])
    problems = tool.check({"c": ["a.rvl"]}, prov)
    assert any("STALE human_authored entry: gone.rvl" in p for p in problems), \
        problems


def test_rewriting_the_manifest_carries_the_authorship_axis(tmp_path):
    tool = _tool()
    prov = _manifest({"0": ["a.rvl"]}, {"c": 0}, human=["a.rvl"])
    out = tmp_path / "m.json"
    tool.write({"c": ["a.rvl"]}, prov, 0, path=out)
    assert json.loads(out.read_text())["human_authored"] == ["a.rvl"]


def test_the_report_does_not_call_the_generation_axis_human():
    """The word that caused issue #1397. The generation table says
    `loop-authored`, and the sentence a reader would take as a human-authorship
    claim lives under the second table, where it is true."""
    tool = _tool()
    prov = _manifest({"0": ["a.rvl"]}, {"c": 0})
    text = tool.report({"c": ["a.rvl"]}, prov)
    assert "loop-authored at or after generation 1" in text
    assert "authorship: documents a person is declared to have typed" in text
