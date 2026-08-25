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

## Lexer before→after — native char-classification builtins (item 233)

The lexer is the heaviest-overhead stage because it does the most work *per
input byte*. Item 233 added single-character ASCII classification builtins
(`is_digit`/`is_alpha`/`is_alnum`/`is_space`, docs/stdlib-2.0.md §Str.is_alnum)
and adopted them in `selfhost/lexer.rvl`'s hot per-byte path. Each classified
byte previously paid a revl-fn call (`is_alnum(c)`) plus a `code0`/`charCodeAt`
round-trip and a code-point range compare; the builtins lower to an inline
native test (a chained comparison / tuple membership), so the call and the
`ord` round-trip drop out. The scan loops (`scan_word`, `scan_digits`) and the
`step` whitespace check now use the builtins directly, and the classification
helpers' bodies defer to them too.

Measured on the same machine (`Darwin-25.2.0-arm64`, CPython 3.14.6), same
corpus, median whole-corpus pass — the correctness gate confirms the self-host
lexer stays **token-for-token identical** to the reference across the change
(it produces the same tokens, only faster):

| lexer stage                     | ref ms | self-host ms | overhead |
|---------------------------------|-------:|-------------:|---------:|
| **before** (item 229 baseline)  |  4.99  |    24.58     | **4.9x** |
| **after** (item 233)            |  4.67  |    20.55     | **4.4x** |

The self-host lexer run drops **~24.6 ms → ~20.5 ms (≈ 17 % faster)** and the
overhead factor falls **4.9x → 4.4x**. Char classification is only a fraction of
the per-byte work (the functional lexer still allocates a 1-char string per
`charAt`, copies records, and slices), so this is the share the native builtins
reclaim; the rest is the value-layer tax that a native tier is what erases.

## Reading the numbers

- **Heaviest overhead: the LEXER — not the emit stage.** The lexer does the
  most accessor indirection *per input byte* (character-by-character scanning
  through the functional value layer), so the CPython tax compounds hardest
  there. This is the stage a native tier will help most. Item 233's native
  char-classification builtins already trimmed it from **4.9x to 4.4x** (see
  the before→after section above); the residue is the per-byte value-layer tax
  (1-char `charAt` allocation, record copies) a native tier erases.
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

## Native tier (rust) — the item-229 "after" (item 266)

The CPython table above is the *before*. The meaningful comparison this doc
defers is the same self-host stages emitted to a fast **native tier (rust)** vs
CPython: how much of the per-access interpreter tax the native tier erases.
`tools/bench_selfhost_rust.py` builds that number. It reuses the exact stage
list and corpus from `tools/bench_selfhost.py` (imported, not copied), and for
each stage: emits the stage to rust through the reference rust backend
(`backends/rust/emit.py`), assembles a runnable cargo binary whose `main` drives
the stage's pure entry point over the corpus, `cargo build --release` ONCE in
setup, then runs the binary once. The binary times the run **in process** with
`std::time::Instant` using the same methodology as the py tier (warmup passes
discarded, median of many whole-corpus passes), so only the run is timed and the
one-time build is a note. rust-vs-CPython is the CPython self-host run time (from
the table above) divided by the native run.

Machine: `Darwin-25.2.0-arm64` (Apple Silicon). Toolchain: **cargo 1.85.1 /
rustc 1.85.1**. cordis-rs resolves here (the same `needs_cordis_rs` gate the
runtime tests use is green: `rust_runtime_reason()` returns `None`), and the
backend's own scenario/router suites prove emitted rust builds and runs on this
box. So this is **not** a toolchain skip.

The number is nonetheless **unmeasured**, for a different and specific reason:
the reference rust backend cannot yet **emit** any of the five self-host stages.
Each stage is refused at emit time by a distinct, real limitation:

| stage    | cpython run ms | rust run ms | rust vs cpython | emit blocker (backends/rust/emit.py) |
|----------|---------------:|------------:|----------------:|--------------------------------------|
| lexer    | 20.55          | unmeasured  | unmeasured      | `unknown builtin method 'is_digit'`: the item-233 char-classification builtins (`is_digit`/`is_alnum`/`is_alpha`/`is_space`) are implemented on the py tier but not the rust tier |
| parser   | 3.74           | unmeasured  | unmeasured      | `cannot infer Rust struct type for record literal with fields ['e', 'i']`: anonymous record literal not resolvable to a named struct |
| checker  | 1.41           | unmeasured  | unmeasured      | `record field identifier collides with Rust/reserved name: 'ctx'`: a stage record field named `ctx` hits the emitter's reserved set |
| lower    | 2.57           | unmeasured  | unmeasured      | `cannot infer Rust struct type for record literal with fields ['i', 'ok', 'xs']`: same anonymous-record class as parser |
| emit_py  | 4.93           | unmeasured  | unmeasured      | `extern py_repr has no @rs body`: the stage depends on a CPython-only extern (`repr`), fundamentally not portable to a native tier |

These are all inside `backends/rust/emit.py` (and, for emit_py, an intentionally
py-only extern in `selfhost/emit_py.rvl`). Closing them is out of scope for this
item; when they close, `tools/bench_selfhost_rust.py` fills the table with no
further change. The harness is committed and verified end to end: a trivial
emittable stage runs the full emit → assemble → `cargo build --release` → run →
median path on this box, so the only missing piece is emitter coverage of the
self-host source.

### Reading for item 231a

Item 231a asks whether the lexer's residual py-tier overhead (4.9x → 4.4x after
item 233's inline char classification) is a **py-only lever** or one a native
tier would erase anyway. This run cannot answer that with a measured native
number yet, and the reason is itself the finding: the rust tier does not
implement the item-233 char-classification builtins at all, so it cannot even
run the lexer's hot per-byte path, let alone show what a native version of it
costs. Until `backends/rust/emit.py` grows those builtins (and the parser /
checker / lower record and reserved-word gaps close), the native "after" for the
lexer stays open, and the 4.4x should be read as a py-tier figure whose native
counterpart is not yet observable. The honest status is "blocked on rust-emitter
coverage", not a factor.

## Reproducing

```
python3 tools/bench_selfhost.py        # CPython py-tier baseline (item 229)
python3 tools/bench_selfhost_rust.py   # native rust tier (item 266)
```

`bench_selfhost.py` prints the machine/CPython line, the correctness-gate
confirmation, the per-stage table, the heaviest-overhead and pre-195 emit
callouts, and the one-time compile-cost note. No third-party dependencies; times
only the run.

`bench_selfhost_rust.py` prints the machine/toolchain line, then per stage emits
to rust, builds a cargo binary once, and times only the run; a stage that cannot
be emitted, built, or run is reported "unable to measure" with the exact reason
(never a fabricated number). It needs cargo and a resolvable cordis-rs; with
neither it skips with the same reason `tests/test_run_rust.py` skips on.
