"""Guard for the tokens-to-green metric (roadmap item 50 — the token economy).

`bench/tokens.py` records **output tokens spent per admitted component** beside
the iterations-to-green that `run.py`/`rescore.py` already track — the number
every token-economy optimization has to move before it can claim it pays. It
recomputes green against the current checker (like `rescore.py`) and sums the
output tokens the model emitted across every attempt up to the admitted one.

This test does NOT pin the exact token counts (they move whenever the checker or
the corpora change). It pins the properties that make the number trustworthy:

  1. The tokeniser is deterministic and total — same bytes, same count, always.
  2. tokens-to-green over a committed corpus is deterministic: same inputs, same
     cells, twice.
  3. The arithmetic ties out: a cell's est_tokens_to_green is exactly the sum of
     the per-attempt counts up to and including its green attempt — retries
     included, nothing past green counted.
  4. The headline mean is taken over admitted cells only.
  5. The exact-output-token hook is wired: a row that carries a recorded
     `output_tokens` is read as such; the committed corpora carry none yet, so
     the metric is honestly on the proxy.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import tokens  # noqa: E402

# a corpus that exists in-tree with multi-attempt cells to sum over.
CORPUS = "typed-deepseek-v4-pro"


def _compute():
    return tokens.compute_run(CORPUS, compiler_root=ROOT)


def test_tokeniser_is_deterministic_and_total():
    samples = [
        "", "\n", "   ", "component C {}\n",
        "let cache = effect Map.new() undo cache.drop()\n",
        "fn leaked_categories(xs: List[Row]) -> Int { return 0 }\n",
    ]
    for s in samples:
        a, b = tokens.count_tokens(s), tokens.count_tokens(s)
        assert a == b, "same input must yield the same count"
        assert isinstance(a, int) and a >= 0
    # non-empty source is non-zero, and a longer identifier costs more than a
    # short one (BPE fragments long runs — the proxy must too).
    assert tokens.count_tokens("component C {}\n") > 0
    assert tokens.count_tokens("leaked_categories") > tokens.count_tokens("fn")


def test_tokens_to_green_is_deterministic_over_a_committed_corpus():
    a = _compute()
    b = _compute()
    key = lambda c: (c["spec"], c["variant"])  # noqa: E731
    a.sort(key=key)
    b.sort(key=key)
    reduce = lambda cells: [(c["spec"], c["variant"], c["green_at"],  # noqa: E731
                             c["est_tokens_to_green"], c["admitted"]) for c in cells]
    assert reduce(a) == reduce(b), "same corpus + checker must give the same cells"
    assert a, "the corpus has cells to score"


def test_est_tokens_to_green_sums_attempts_up_to_green():
    """A cell's number is exactly the per-attempt counts summed to green — the
    retries are paid for, nothing after green is counted."""
    cells = _compute()
    run_dir = tokens.rescore.RESULTS / CORPUS
    checked_multi = False
    for c in cells:
        variant_dir = run_dir / c["spec"] / c["variant"]
        attempts = tokens._attempts_in_order(variant_dir)
        # how many attempts feed the sum: up to green, or all of them if unsolved
        upto = c["green_at"] if c["admitted"] else len(attempts)
        expected = sum(tokens.count_tokens(p.read_text()) for p in attempts[:upto])
        assert c["est_tokens_to_green"] == expected
        if upto > 1:
            checked_multi = True
    assert checked_multi, "sanity: the corpus has at least one multi-attempt cell"


def test_headline_mean_is_over_admitted_cells_only():
    cells = _compute()
    admitted = [c for c in cells if c["admitted"]]
    assert admitted, "the corpus admits some components"
    # every admitted cell has a concrete green attempt; unadmitted ones do not.
    assert all(c["green_at"] is not None for c in admitted)
    assert all(c["green_at"] is None for c in cells if not c["admitted"])
    doc = tokens.to_json([CORPUS], cells, sha="test")
    mean = doc["headline"]["mean_est_tokens_to_green"]
    hand = sum(c["est_tokens_to_green"] for c in admitted) / len(admitted)
    assert abs(mean - hand) < 1e-9
    assert doc["headline"]["admitted_components"] == len(admitted)


def test_recorded_output_token_hook_is_wired_but_the_corpus_is_on_the_proxy():
    # the committed corpus carries no recorded output_tokens -> proxy only.
    assert tokens._recorded_output_tokens(CORPUS) == {}
    cells = _compute()
    assert all(c["recorded_tokens_to_green"] is None for c in cells)
    doc = tokens.to_json([CORPUS], cells, sha="test")
    assert doc["headline"]["any_recorded_output_tokens"] is False


def test_recorded_output_tokens_are_read_when_present(tmp_path, monkeypatch):
    """When a funded run records `output_tokens` per attempt, the loader reads
    them — keyed by (spec, variant, attempt), summary rows ignored."""
    run = tmp_path / "results" / "funded"
    (run).mkdir(parents=True)
    (run / "results.jsonl").write_text(
        '{"spec": "01-kv-provider", "variant": "v1", "attempt": 1, '
        '"ok": false, "output_tokens": 120}\n'
        '{"spec": "01-kv-provider", "variant": "v1", "attempt": 2, '
        '"ok": true, "output_tokens": 95}\n'
        '{"spec": "01-kv-provider", "variant": "v1", "summary": true, '
        '"green_at": 2, "output_tokens": 999}\n'
    )
    monkeypatch.setattr(tokens.rescore, "RESULTS", tmp_path / "results")
    recorded = tokens._recorded_output_tokens("funded")
    assert recorded == {
        ("01-kv-provider", "v1", 1): 120,
        ("01-kv-provider", "v1", 2): 95,
    }
