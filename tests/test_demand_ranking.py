"""Guard for the refusal-demand miner (roadmap item 38).

`bench/demand.py` extends the `rescore.py` failure taxonomy into a *ranked
demand table*: it recompiles the committed `bench/results/` corpora, mines the
symbol each refusal shows the model reached for, and ranks those reached-for
symbols by frequency so the stdlib/syntax roadmap can be ordered by measured
demand. An opt-in second source folds in captured MCP session diagnostics.

This test does NOT pin the exact counts (they move whenever the checker or the
corpora change). It asserts the properties that make the ranking usable:

  1. It produces a non-empty ranking from a committed corpus, and every row's
     count is the length of its refusal log (the arithmetic ties out).
  2. The symbol extractor splits the taxonomy's conflated `(G6, guarantee)`
     bucket into a distinct *stdlib-method* demand and *syntax-form* demand.
  3. The ranking is deterministic: same inputs, same order, twice — and sorted
     by descending count with a stable tie-break.
  4. The opt-in MCP source is wired but OFF by default: mining a corpus alone
     never reads MCP, and passing a capture folds its refusals in, tagged with
     an `mcp` source.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import demand  # noqa: E402

# a corpus that exists in-tree and carries refusals to mine.
CORPUS = "typed-deepseek-v4-pro"


def _mine():
    return demand.mine_corpus([CORPUS], attempts="all", compiler_root=ROOT)


def test_produces_a_ranked_table_from_a_committed_corpus():
    table, refusals = _mine()
    ranked = demand.rank(table)
    assert ranked, "the corpus has refusals; the ranking must not be empty"
    # every row's count equals how many refusals fed it — the arithmetic ties.
    assert sum(d.count for d in ranked) == len(refusals)
    for d in ranked:
        assert d.count == sum(d.codes.values())
        assert d.example  # a representative example was captured


def test_splits_the_conflated_taxonomy_bucket():
    """The taxonomy lumps `no builtin method take` and the parse error
    `found 'kv'` into one (G6, guarantee) bucket; demand mining splits them."""
    table, _ = _mine()
    kinds = {(d.kind, d.symbol) for d in table.values()}
    assert ("stdlib-method", "Str.take") in kinds
    assert ("syntax-form", "kv") in kinds


def test_ranking_is_deterministic_and_ordered():
    table_a, _ = _mine()
    table_b, _ = _mine()
    order_a = [(d.kind, d.symbol, d.count) for d in demand.rank(table_a)]
    order_b = [(d.kind, d.symbol, d.count) for d in demand.rank(table_b)]
    assert order_a == order_b, "same inputs must yield the same order"
    # descending by count, with a fully-determined tie-break (no equal keys).
    keys = [(-d.count, demand._KIND_ORDER.get(d.kind, 9), d.kind, d.symbol)
            for d in demand.rank(table_a)]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys)), "the sort key is a total order"


def test_mcp_source_is_opt_in_and_off_by_default():
    # off by default: mining the corpus alone records only corpus refusals.
    table, refusals = _mine()
    assert refusals, "sanity: the corpus has refusals"
    assert all(r["source"] == "corpus" for r in refusals)
    assert all(set(d.sources) == {"corpus"} for d in table.values())


def test_mcp_source_folds_in_when_given(tmp_path):
    table, refusals = _mine()
    baseline = len(refusals)

    capture = tmp_path / "session.jsonl"
    capture.write_text(
        # a report() doc, a bare classify() record, and a restore-error shape.
        '{"ok": false, "diagnostics": [{"code": "HOST-METHOD", '
        '"category": "host-boundary", "message": "`Map` has no method `merge` '
        '(its surface: get, set)"}]}\n'
        '{"code": "G6", "category": "guarantee", "message": "no builtin method '
        '`trim` on `Str` — the stdlib surface is slice"}\n'
        '{"diagnostic": {"code": "T1", "category": "parse", "message": '
        '"expected `)`, found \'yield\'"}}\n',
        encoding="utf-8",
    )

    folded = demand.mine_mcp([str(capture)], table, refusals)
    assert folded == 3
    assert len(refusals) == baseline + 3

    by_symbol = {(d.kind, d.symbol): d for d in table.values()}
    merge = by_symbol[("host-method", "Map.merge")]
    assert merge.sources["mcp"] == 1 and "corpus" not in merge.sources
    # a symbol only the MCP source reached for is now roadmap-actionable.
    assert ("syntax-form", "yield") in by_symbol
    assert ("stdlib-method", "Str.trim") in by_symbol


def test_classify_demand_extracts_each_refusal_kind():
    cases = [
        (("HOST-METHOD", "host-boundary", "`Map` has no method `merge` (its surface: get)"),
         ("host-method", "Map.merge")),
        (("HOST-ARITY", "host-boundary", "host `Map.get` takes 1 argument, got 2"),
         ("host-method", "Map.get")),
        (("G6", "guarantee", "no builtin method `take` on `Str` — the stdlib surface is slice"),
         ("stdlib-method", "Str.take")),
        (("T1", "parse", "expected a statement, found 'kv'"),
         ("syntax-form", "kv")),
        (("T1", "type-mismatch", "`Cache` is not a type — revl has no tuples"),
         ("missing-type", "Cache")),
    ]
    for (code, category, message), expected in cases:
        assert demand.classify_demand(code, category, message) == expected
    # no symbol to mine -> ranked under its guarantee, never dropped.
    kind, _ = demand.classify_demand("G4", "emission-propagation", "`Cache.put` is declared plain")
    assert kind == "guarantee:G4"
