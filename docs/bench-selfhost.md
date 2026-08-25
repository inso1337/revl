# Self-host compiler overhead — the CPython py-tier baseline (item 229)

The self-host compiler stages (`selfhost/*.rvl`) are *written* in revl but *run*
by being compiled to Python through the reference python backend
(`backends/python/emit.py`) and executed on CPython. This document is the
committed **baseline** for the known overhead of that CPython-emitted self-host
LOGIC against the hand-written reference stage, measured per stage by
`tools/bench_selfhost.py`.

## What this measures (and what it does NOT)

The self-host stages are pure, functional revl. They navigate the token stream /
AST / IR by value-copying and through `value_*` (stdlib/value.rvl) and `@py`
accessor indirection rather than by mutating Python objects in place. Emitted to
CPython, that indirection is a **constant tax** on top of the reference's direct
Python. The factor below is the price of the **CPython tier** — it is **not a
verdict** on revl or on the self-host design.

The meaningful future comparison is the self-host compiler emitted to a fast
**NATIVE tier (rust/go)** vs this CPython baseline; the native tier erases the
per-access interpreter overhead this baseline captures. This file exists so that
comparison has a *before*.

The **emit_py** row is additionally the **pre-195 baseline**: item 195's
render-context (state-threading) change targets exactly the emit stage's
accessor/threading cost, so its factor here is the number a future before/after
compares against.

## Methodology (fair + reproducible)

- **Compile once, time only the run.** Each self-host stage is compiled
  revl→py and `exec`'d exactly once in setup (reusing the machinery in
  `tests/test_selfhost_*.py`). Only the RUN of the resulting stage function is
  timed, isolating the self-hosted *logic's* overhead from the one-time
  revl→py compile cost (reported separately as a note).
- **Same input both sides + a correctness gate.** Identical input feeds the
  self-host stage and the reference stage; before any timing the tool asserts
  the two produce **equal output** (token-identical / S-expression-identical /
  same inferred type / same admit verdict / **byte-identical** emitted Python),
  so both sides are timed doing equivalent work. These are the same corpora the
  differential stage tests already hold to agreement.
- **Warm + repeated, median.** Warmup passes are discarded; the tool reports the
  **median** whole-corpus pass time over many repeated passes, and the overhead
  **FACTOR** = self-host ÷ reference.
- **Entry-point scope note.** Stages are compared at their existing self-host
  entry points, whose scope differs: `lexer`/`emit_py` are pure single-stage
  (source→tokens, IR→py); `parser`/`checker`/`lower` entry points take *source*
  and internally run the earlier front-end stages too (lex→parse[→check→lower
  gate]). Each factor is the overhead of that stage's entry point as it exists;
  `emit_py` (IR→py) is the cleanest pure-stage isolation and the headline.

## Corpus

- **lexer** — 8 real source files (743 LOC): `examples/{migrator,pulse,user_cache,beacon,tenants}.rvl`,
  `backends/rust/scenarios/probe.rvl`, `tests/fixtures/triple_string.rvl`, and —
  the largest representative program — the lexer's own 416-LOC source
  (self-application).
- **parser** — 101 expressions spanning the full precedence / associativity /
  call / field / index / match / arrow / template / optional-chaining surface
  (the parser differential's accepted corpus).
- **checker** — 59 expressions over the fixed five-binding environment (the
  checker differential's accepted corpus).
- **lower/admit** — 6 whole component/service programs the gate admits (the
  lower differential's `ACCEPTED_PROGRAMS`).
- **emit_py** — the 17-document emit corpus (`tests/fixtures/emit_py_corpus/*`,
  255 LOC): the function + component/service + module-declaration + externs /
  config / method-effect surface that emits byte-identical.

## Baseline results

Machine: `Darwin-25.2.0-arm64` (Apple Silicon). Runtime: **CPython 3.14.6**.
Repo: `origin/main` @ `f8ec6de`. Numbers are the median whole-corpus pass; the
factor is stable to ±0.1x across runs.

| stage        | corpus                    | ref ms | self-host ms | overhead |
|--------------|---------------------------|-------:|-------------:|---------:|
| lexer        | 8 files / 743 LOC         |  5.12  |    24.87     | **4.9x** |
| parser       | 101 exprs                 |  1.47  |     3.74     | **2.5x** |
| checker      | 59 exprs                  |  0.68  |     1.41     | **2.1x** |
| lower/admit  | 6 programs                |  1.34  |     2.57     | **2.0x** |
| emit_py      | 17 IR docs / 255 LOC      |  2.88  |     4.93     | **1.7x** |
| **TOTAL**    | (sum of stage medians)    | 11.49  |    37.52     | **3.3x** |

One-time revl→py compile cost per stage (setup only, excluded from the timed
run): lexer ≈ 24 ms, parser ≈ 82 ms, checker ≈ 200 ms, lower ≈ 268 ms,
emit_py ≈ 118 ms.

## Reading the numbers

- **Heaviest overhead: the LEXER, ~4.9x — not the emit stage.** The lexer does
  the most accessor indirection *per input byte* (character-by-character
  scanning through the functional value layer), so the CPython tax compounds
  hardest there. This is the stage a native tier will help most.
- **emit_py is the LIGHTEST overhead, ~1.7x.** IR→Python is comparatively
  coarse-grained per unit of output, so the per-access tax is proportionally
  smaller. **This 1.7x is the pre-195 baseline** — item 195's render-context
  change targets the emit stage's state-threading, and a future run of this same
  benchmark is the before/after.
- **Aggregate ≈ 3.3x** across the measured stages (a sum of per-stage medians
  over heterogeneous corpora, not a single composed pipeline input — the
  self-host stages are individually differential-tested, not wired into one
  composed py callable, so no single end-to-end self-host callable exists to
  time; the total is the honest aggregate of the parts).

## Reproducing

```
python3 tools/bench_selfhost.py
```

Prints the machine/CPython line, the correctness-gate confirmation, the
per-stage table, the heaviest-overhead and pre-195 emit callouts, and the
one-time compile-cost note. No third-party dependencies; times only the run.
