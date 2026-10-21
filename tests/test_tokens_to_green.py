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
  6. Per-invocation `turn_count` and `tokens_in` stay paired in the report; old
     corpora remain unavailable rather than receiving inferred values.
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
        expected = sum(tokens.count_tokens(p.read_text(encoding="utf-8")) for p in attempts[:upto])
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
    assert tokens._recorded_attempt_metrics(CORPUS) == {}
    cells = _compute()
    assert all(c["recorded_tokens_to_green"] is None for c in cells)
    assert all(c["recorded_turn_count_to_green"] is None for c in cells)
    assert all(c["recorded_tokens_in_to_green"] is None for c in cells)
    doc = tokens.to_json([CORPUS], cells, sha="test")
    assert doc["headline"]["any_recorded_output_tokens"] is False
    assert doc["headline"]["any_recorded_turn_count"] is False
    assert doc["headline"]["any_recorded_tokens_in"] is False


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


def test_per_attempt_turns_and_tokens_in_remain_paired_through_green(
    tmp_path, monkeypatch,
):
    results = tmp_path / "results"
    variant_dir = results / "funded" / "01-kv-provider" / "v1"
    variant_dir.mkdir(parents=True)
    (variant_dir / "attempt-1.rvl").write_text("not green\n")
    (variant_dir / "attempt-2.rvl").write_text("green\n")
    (results / "funded" / "results.jsonl").write_text(
        '{"spec":"01-kv-provider","variant":"v1","attempt":1,'
        '"turn_count":2,"tokens_in":120}\n'
        '{"spec":"01-kv-provider","variant":"v1","attempt":2,'
        '"turn_count":1,"tokens_in":80}\n'
        '{"spec":"01-kv-provider","variant":"v1","summary":true,'
        '"green_at":2,"turn_count_to_green":3,"tokens_in_to_green":200}\n'
    )
    monkeypatch.setattr(tokens.rescore, "RESULTS", results)
    monkeypatch.setattr(tokens, "_load_compiler", lambda root: (None, None, None))
    monkeypatch.setattr(
        tokens.rescore, "score_one",
        lambda path, *args: {"ok": path.read_text(encoding="utf-8").strip() == "green"},
    )

    cells = tokens.compute_run("funded", compiler_root=ROOT)
    cell = next(c for c in cells if c["variant"] == "v1")

    assert cell["attempt_metrics"] == [
        {"attempt": 1, "turn_count": 2, "tokens_in": 120},
        {"attempt": 2, "turn_count": 1, "tokens_in": 80},
    ]
    assert cell["recorded_turn_count_to_green"] == 3
    assert cell["recorded_tokens_in_to_green"] == 200

    report = "\n".join(tokens.render(["funded"], cells, sha="test"))
    assert "model turns and tokens_in by invocation" in report
    assert "| `funded/01-kv-provider/v1` | 1 | 2 | 120 | 60 |" in report
    assert "| `funded/01-kv-provider/v1` | 2 | 1 | 80 | 80 |" in report


def test_unadmitted_cell_has_no_input_metrics_to_green(tmp_path, monkeypatch):
    results = tmp_path / "results"
    variant_dir = results / "funded" / "01-kv-provider" / "v1"
    variant_dir.mkdir(parents=True)
    (variant_dir / "attempt-1.rvl").write_text("still red\n")
    (results / "funded" / "results.jsonl").write_text(
        '{"spec":"01-kv-provider","variant":"v1","attempt":1,'
        '"turn_count":2,"tokens_in":120}\n'
    )
    monkeypatch.setattr(tokens.rescore, "RESULTS", results)
    monkeypatch.setattr(tokens, "_load_compiler", lambda root: (None, None, None))
    monkeypatch.setattr(tokens.rescore, "score_one",
                        lambda path, *args: {"ok": False})

    cell = next(c for c in tokens.compute_run("funded", compiler_root=ROOT)
                if c["variant"] == "v1")

    assert cell["admitted"] is False
    assert cell["recorded_turn_count_to_green"] is None
    assert cell["recorded_tokens_in_to_green"] is None
