# tokens-to-green — the token economy's measurement (roadmap item 50)

*The house rule: measured, not assumed.* This is the committed snapshot of the
number every token-economy optimization must move — **output tokens spent per
admitted component** — computed deterministically from the committed corpora.
Regenerate it (free, no model) with:

```sh
python3 bench/tokens.py                 # both model runs
python3 bench/tokens.py --json t.json   # machine-readable
```

The table names the compiler sha it was measured at; re-run after a checker
change and the number moves with the language, the same way `rescore.py`'s
first-pass rate does.

---

## tokens-to-green — output tokens spent per admitted component
compiler: `8ffb390`  ·  reproduce: `python3 bench/tokens.py`

The token every optimization must move: output tokens the model emitted across
ALL attempts, up to and including the admitted one (retries included — a refused
draft still cost its tokens). `est.` is a deterministic BPE-proxy over the
committed generation files; **as-run $** is the real billed cost from the corpus.
Green is recomputed against the current checker.

| run | variant | admitted | mean est. tokens-to-green | median | total est. | as-run $ |
|---|---|---|---:|---:|---:|---:|
| typed-deepseek-v4-pro | v1 | 23/30 | 139 | 126 | 3197 | $0.0328 |
| typed-deepseek-v4-pro | v2 | 23/30 | 152 | 144 | 3496 | $0.0560 |
| typed-deepseek-v4-pro | v2host | 24/30 | 151 | 130 | 3623 | $0.0659 |
| baseline-deepseek-v4-pro | v1 | 22/30 | 133 | 124 | 2917 | $0.0271 |
| baseline-deepseek-v4-pro | v2 | 21/30 | 151 | 144 | 3161 | $0.0542 |
| baseline-deepseek-v4-pro | v2host | 23/30 | 170 | 129 | 3908 | $0.0558 |

### headline
Across **136** admitted components in
typed-deepseek-v4-pro+baseline-deepseek-v4-pro: **mean 149 est. output
tokens-to-green** (median 130, total 20302), real as-run cost $0.2917.

### costliest admitted cells (est. output tokens-to-green)

- `baseline-deepseek-v4-pro/02-pg-pool/v2host` — 457 tokens over 3 attempt(s)
- `baseline-deepseek-v4-pro/13-provider-consumer-pair/v2host` — 421 tokens over 2 attempt(s)
- `typed-deepseek-v4-pro/29-mesh/v2` — 244 tokens over 2 attempt(s)
- `typed-deepseek-v4-pro/29-mesh/v1` — 243 tokens over 2 attempt(s)
- `typed-deepseek-v4-pro/09-warmup-cache/v2` — 242 tokens over 1 attempt(s)
- `typed-deepseek-v4-pro/29-mesh/v2host` — 237 tokens over 2 attempt(s)
- `baseline-deepseek-v4-pro/09-warmup-cache/v2` — 235 tokens over 1 attempt(s)
- `typed-deepseek-v4-pro/30-saga-transfer/v2host` — 229 tokens over 1 attempt(s)

### real vs needs-a-funded-run

- **real now:** the generation artefacts (every `attempt-N.rvl`) and the as-run
  dollar cost are committed and exact.
- **proxy now:** `est. tokens` is a deterministic BPE-shaped estimate over those
  artefacts — the model's own output-token count was never recorded for these
  corpora.
- **needs a funded run:** no committed cell carries a recorded `output_tokens`
  yet. `run.py` now captures cline's `output_tokens` per attempt, so the next
  paid run replaces the proxy with the exact figure with no code change here.

---

## What this number does and does not settle

- It is the **denominator for every optimization** in item 50's other three
  bullets. A compound MCP verb, a terser wire-form, or `revl_edit` structured
  patches each claims to cut tokens; this metric is what says by how much, on
  the same corpus, before and after. No trick ships on taste.
- The **generation-side** figure above is only the *output* half of the token
  economy. The larger, unmeasured half is the **protocol-side** spend — the
  tokens an agent burns re-sending source and running chatty verb sequences
  through the MCP surface. That is what the companion audit ranks:
  [`token-surface-audit.md`](token-surface-audit.md).
- Where the paying tricks eventually land is the "How you're measured" contract
  in `docs/guide-ai-agents.md`; this metric is the scale they are weighed on.
