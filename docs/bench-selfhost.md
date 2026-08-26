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

## Lexer before/after: emitter inlining of small pure fns (item 231a)

Item 231a adds a **py-tier inlining pass** to `backends/python/emit.py`: a small,
pure, non-recursive helper fn with the guarded-return shape (`if (c) { return X }`
guards then one terminal `return Y`) and at most one parameter is folded into its
call sites inside `fn` bodies, so a hot loop pays an inline comparison instead of
a CPython call frame. It is deliberately conservative and behavior-preserving:
only helpers whose fully-expanded body references nothing but their parameter,
and only where substituting the argument cannot change evaluation (used once in
an always-evaluated spot; or, when duplicated, effect-free). The native tiers
already inline these, so this is the py-tier equivalent. The self-host lexer's
per-character helpers (`code0`, `is_alpha`, `is_alnum`, `is_digit`, `is_space`,
`is_ws`) are exactly the shape the pass folds, and it inlines every one of them
(tokens stay identical to the reference across the change, the correctness gate).

Measured on the same machine (`Darwin-25.2.0-arm64`, CPython 3.14.6), same
methodology, median of repeated whole-corpus passes:

| lexer stage                     | ref ms | self-host ms | overhead |
|---------------------------------|:------:|:------------:|:--------:|
| **before** (item 233)           |  5.20  |    25.3      | **4.9x** |
| **after** (item 231a inlining)  |  5.14  |    24.8      | **4.8x** |

The honest finding: on the **current** self-host lexer the movement is inside
run-to-run noise (±0.1x). The reason is not that inlining fails, it is that the
lexer's premise (item 229's "per-byte `is_alnum(charAt(j))` fn-call layering")
no longer holds in the source: `selfhost/lexer.rvl`'s hottest loops (`scan_word`,
`scan_digits`, the main `step` scan) were already hand-rewritten to call the
item-233 char builtins directly (`source.charAt(j).is_alnum()`), so the fn calls
the pass removes (`code0`, `is_alpha`) now sit on the colder per-token paths, a
smaller share of the per-byte work.

The pass does deliver where the per-call pattern is still present. A synthetic
hot loop that classifies every byte through the helper fns (the pattern the
lexer used to have) runs **1.22x faster** inlined than called (14.6 ms vs 17.8 ms
over the same input, output identical), which is the general py hot-loop lever
231a targets: any revl-on-py code that still calls a small pure helper per
iteration gets that saving, even though the self-host lexer has already captured
most of its own share by hand. `parser`/`checker`/`lower` move within noise too
(their hot paths are not per-byte helper calls).

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

Since item 270 landed (`_by_value_arg`/`_by_value_tail` clone-on-reuse) the
**lexer** now emits, `cargo build --release`es, and runs on this box, so the
lexer row is measured. The other four stages still do not build and stay
unmeasured, each for a distinct, real reason:

| stage    | cpython run ms | rust run ms | rust vs cpython | status (backends/rust/emit.py) |
|----------|---------------:|------------:|----------------:|--------------------------------|
| lexer    | 20.55          | **348.8**   | **0.06x (17x SLOWER)** | measured; the run is dominated by per-access string cost, not native speed (see item 277 below) |
| parser   | 3.74           | unmeasured  | unmeasured      | `cargo build` fails: `E0072` recursive ADT needs `Box`, plus residual `E0382` move shapes (let-RHS / `Vec` `into_iter` twice) and `E0308`/`E0609`/`E0282` (item 278) |
| checker  | 1.41           | unmeasured  | unmeasured      | same `E0072`/`E0382` build gaps as parser (item 278) |
| lower    | 2.57           | unmeasured  | unmeasured      | same `E0072`/`E0382` build gaps as parser (item 278) |
| emit_py  | 4.93           | unmeasured  | unmeasured      | `extern py_repr has no @rs body`: the stage depends on a CPython-only extern (`repr`), fundamentally not portable to a native tier |

The parser/checker/lower build gaps are item 278; the emit_py gap is an
intentionally py-only extern in `selfhost/emit_py.rvl`. The harness is verified
end to end: the lexer runs the full emit → assemble → `cargo build --release` →
run → median path on this box (one-time build ~7s, reported as a note).

### The lexer is 17x SLOWER than CPython, and per-function `Vec<char>` materialisation does not fix it (item 277)

The measured lexer is **348.8 ms native vs 20.55 ms CPython** — the native tier
is ~17x *slower*, not faster. Item 270's native run attributed this to `charAt`
/`charCodeAt` lowering to `str::chars().nth(i)`, which re-walks the `String` from
the front on every access: a Rust `String` has no O(1) codepoint index, so the
lexer's positional per-character scan is O(n^2). The py tier does not share the
bug (`s[i]` is O(1) under PEP 393), so it is rust-specific.

Item 277 tried the established fix: materialise each positionally-indexed `Str`
once per function as `let <name>_cs: Vec<char> = <name>.chars().collect();` and
lower each `charAt(i)` to `<name>_cs[i as usize]` (O(1) after one O(n) collect),
indexing by Unicode scalar to preserve `chars().nth` semantics. **It regressed
the benchmark**, consistently and measurably:

| lexer native run                          | ms     |
|-------------------------------------------|-------:|
| before (origin/main, `chars().nth`)       | 348.8  |
| after (per-function `Vec<char>` shadow)   | 444.9  |
| materialise only `step` (per-token)       | 422.0  |
| materialise only `scan_word`              | 356.0  |

The cause is the lexer's architecture, not the lowering. `selfhost/lexer.rvl`
does not index one string in one loop in one function; it threads `source`
(up to 16.8 KB in the corpus) as a parameter through a dozen per-token helpers
(`step`, `scan_word`, `scan_digits`, `scan_string`, …). A *per-function* shadow
therefore becomes a *per-call* `source.chars().collect()`: `step` alone is called
once per token and collects the whole source every call, so the collect is
O(tokens · n) ≈ O(n^2) of pure allocation and UTF-8 decoding that its short
per-call scan never amortises. On this corpus size the allocation cost of the
collect exceeds the iteration cost of `chars().nth`, so every subset regresses —
even materialising only `scan_word` is +6 ms, and no subset is a net win. The
per-function shadow that works for a single self-contained scan loop is the wrong
shape for a scanner that hands its buffer down a call chain.

The genuine fix is **interprocedural**: collect `source` into a `Vec<char>`
*once* at the outermost owner and pass an indexable `&[char]` view down the scan
helpers, so the O(n) collect is paid a single time per lex rather than per token.
That is a call-site / signature transform (or a lexer change to thread the view),
materially larger and riskier than the per-function shadow item 277 scoped — and
it collides with `source`'s other uses (`slice`/`length`/equality/pass-through),
which still need the `String`. It should be a separate, carefully-designed item;
a public function cannot silently grow a synthetic `&[char]` parameter without
breaking its callers. This item ships no emitter change: the per-function shadow
is reverted so the lexer stays at 348.8 ms with byte-identical output, and the
regression and its mechanism are recorded here rather than shipped.

A second, open question this surfaces: 17x-slower is a large gap for a native
tier, and the per-access `charAt` cost is only part of it. `selfhost/lexer.rvl`'s
hot path also leans on `source.slice(..)` (another front-of-string char walk) and
per-token record copies — the value-layer tax item 276 targets — so even a
correct O(1) `charAt` may not by itself make the native lexer competitive. The
honest status is that the native lexer is measured, slow, and not yet explained
by `charAt` alone.

### item 283: where the 17x goes

Item 277 closed with the native lexer measured, slow, and "not yet explained by
`charAt` alone". Item 283 explains it, with data. The measurement tool is
`tools/profile_selfhost_rust.py` (reproducible, no sudo): it re-emits the exact
item-266 lexer crate and does three things, a build-profile sweep, an
instrumented per-operation decomposition, and micro-benchmarks that price each
hot operation at its observed frequency, then cross-checks the split with a
macOS `sample` run of the release binary. Toolchain and corpus are the item-266
ones: `Darwin-25.2.0-arm64`, cargo 1.85.1 / rustc 1.85.1, the same 8-file /
27,832-char lexer corpus, in-process `Instant` median.

**The headline: it is not the build profile, and it is not `charAt`. It is
`revl_push` cloning the whole accumulator on every append, an O(tokens^2) deep
copy that is ~85% of the run.**

**1. Build profile (ruled out first, cheapest check).** The item-266 harness
runs `cargo build --release` against a `Cargo.toml` (`backends/rust/emit.py`
`cargo_toml()`) that declares NO `[profile.release]` block, so it inherits
cargo's default release profile: opt-level 3, debug-assertions off,
overflow-checks off, lto off, codegen-units 16. That is a genuine optimized
build, confirmed by sweeping the knobs:

| profile                                   | lexer run ms | note |
|-------------------------------------------|-------------:|------|
| release-default (what item 266 builds)    | 350.3        | opt-level 3, overflow-checks off |
| release + `overflow-checks=false` explicit| 352.3        | no change (within noise) |
| release + `lto="fat"` + `codegen-units=1` | 333.9        | ~5% faster, not the gap |
| release + `opt-level=2`                    | 412.4        | worse |
| dev (unoptimised, opt-level 0)            | 6820.8       | ~19x the release build |

Two conclusions. The dev row proves the harness is NOT accidentally building
debug (a debug build would be ~6.8s, not 0.35s). And overflow-checks are already
off and make no difference here anyway, because the rust backend emits `Int`
arithmetic as explicit `checked_add(...).expect("revl: Int overflow")`
(`docs/arithmetic.md`, Int overflow traps by contract), so overflow trapping
lives in the emitted code regardless of the profile flag. lto+codegen-units=1
buys ~5%. The 17x is not a build-profile artifact.

**2. Per-operation decomposition (instrumented build, one exact pass).** A
counting global allocator plus atomic counters injected into the hot operations.
One whole-corpus pass is deterministic, so these counts are exact:

| operation (per whole-corpus pass)            | count | note |
|----------------------------------------------|------:|------|
| **`revl_push` elements copied (clone-append)** | **5,912,806** | **O(tokens^2); each Token clone = 2 String allocs = ~11.8M allocs = 98% of all allocations** |
| `charAt` big-string `chars().nth` (O(i))     | 34,538 | summed index depth 206,713,954 chars walked, avg depth ~5,985 |
| 1-char `String` allocs (`charAt.to_string`)  | 34,538 | the item-276 value-layer alloc |
| `revl_slice` calls                           | 17,883 | only 43,046 chars collected, avg ~2 chars (cheap) |
| full-source `.clone()` (threaded to helpers) | 17,397 | 221.7 MB memcpy / pass |
| `revl_length` (`chars().count`, O(n))        | 2,724 | |
| `revl_concat`                                | 1,245 | |
| **total heap allocations / pass**            | **12,085,346** | |
| **total heap bytes / pass**                  | **1,249.3 MB** | |

12 million allocations per pass over a 27 KB corpus is ~434 allocations per
source character. The counters attribute 98% of them to one operation:
`revl_push`. The persistent-list append lowering in `backends/rust/emit.py` is

```rust
fn revl_push(&self, item: T) -> Vec<T> { let mut _v = self.clone(); _v.push(item); _v }
```

and the lexer's accumulator loop is `out = out.revl_push(s.tok)`, once per
token. Each push deep-clones the entire growing `Vec<Token>`, and cloning a
`Token { kind: String, text: String, line: i64 }` allocates both of its Strings.
Appending token k therefore copies k Tokens = 2k String allocations, so the
whole lex is O(tokens^2) allocation: 4,796 pushes copy 5.9M elements and allocate
11.8M Strings. This is rust-specific: the CPython tier's list copy duplicates
pointers under reference counting, not the string contents, so the same value
semantics cost almost nothing there.

**3. Micro-benchmarks (each op priced at its observed frequency).** Standalone
rust, same corpus scale:

| priced operation                                             | ms / pass | share |
|--------------------------------------------------------------|----------:|------:|
| **(E) `revl_push` clone-on-append, 5.9M Token clones**       | **~300.0** | **~85%** |
| (A) `charAt` front-walk `chars().nth(i).to_string()` x34,538 | ~9.6      | ~2.7% |
| (C) full-source `String::clone` x17,397                      | ~6.2      | ~1.8% |
| (D) `revl_slice` x17,883 @ avg 2 chars                       | ~0.6      | ~0.2% |
| (B) same char accesses vs a `Vec<char>` O(1) index           | ~0.02     | control |

A/C/D/E sum to ~316 ms of the ~350 ms run; the rest is Token/Scan/Step struct
construction, `String::from` literals in the comparison chains, and Vec buffer
reallocations. A macOS `sample` of the release binary agrees at the leaf level:
about 66% of samples are in `malloc`/`free` (`_xzm_xzone_malloc_tiny`, `_xzm_free`,
`_malloc_zone_malloc`) and ~18% in `memmove`/`memcpy` (String clone and collect
copies), with the whole lexer inlined into `lex_src` whose heaviest child frame
is `revl_push`. The O(n^2) `charAt` walk (`Chars::advance_by`) is ~2.3% of leaf
time, which is the same signal control (B) gives: replacing the front-walk with an
O(1) `Vec<char>` index takes the char-access cost from ~9.6 ms to ~0.02 ms, a real
but ~3% win.

**The single highest-leverage target.** Eliminate the clone-on-append in
`revl_push`. When the receiver is dead after the call, as in `out =
out.revl_push(x)` where `out` is immediately reassigned, the value-semantics
clone is provably unnecessary: a last-use / linear move that mutates the Vec in
place (`Vec::push`) is O(1) amortized and preserves semantics. That single change
targets the ~85% of the run the accumulator copy owns, and it is not specific to
the lexer, it is every persistent-collection append the rust backend emits.

This re-routes the earlier candidates. Item 276 (codepoint scan / per-char
1-char alloc) addresses operation A, ~3%. Item 282 (interprocedural `&[char]`
view) addresses A plus C, up to ~5% combined if the view also removes the
by-value `String` passing. Both are real and worth doing, but neither is the
dominant cost. The dominant cost, `revl_push` clone-on-append, is covered by
neither and should be its own item: a move/last-use optimization for value-append
on a dead receiver in `backends/rust/emit.py`. This is a data-driven finding
only; item 283 ships no emitter change. Reproduce with
`python3 tools/profile_selfhost_rust.py`.

### item 284: the clone-elision fix (the ~85% recovered)

Item 284 ships the emitter change item 283 routed. When a persistent-collection
append is bound straight back to its own receiver, `out = out.push(x)`, the
pre-image of `out` is overwritten and never read again, so the value-semantics
copy is dead work. `backends/rust/emit.py` now lowers that one shape to an
in-place `out.push(x);` (and the Map siblings `m = m.set(k, v)` / `m = m.remove(k)`
to `m.insert(..)` / `m.remove(..)`), which is O(1) amortised instead of copying
the whole growing buffer per token.

The rewrite fires only for the self-reassignment `assign` where the target and
the call receiver name the same bare local. That is the shape it can prove both
dead (the assignment rebinds the local over its own value) and uniquely owned:
every persistent method borrows its receiver (`&self` / `self.clone()`), so a
second live owner of the buffer could only come from a by-value move of `out`,
and every such move the backend emits already clones first
(`_by_value_arg`/`_by_value_tail`, record-field and closure captures) or, for a
bare `let a = out` that then reuses `out`, fails to compile today (E0382). So in
exactly the cases that compile, `out` is the sole owner and the in-place write
yields the identical value. `concat` is left alone because it resolves to Str or
List and its receiver type is not known at the call node.

Same tool, same 27,832-char corpus, same box, back to back:

| measure (whole-corpus pass)             | before (283) | after (284) | change        |
|-----------------------------------------|-------------:|------------:|---------------|
| lexer run, release-default              | 351.43 ms    | 28.12 ms    | 12.5x faster  |
| rust vs CPython (20.55 ms)              | 0.06x (17x slower) | 0.73x (1.37x slower) | roughly linear |
| `revl_push` clone-append calls          | 4,796        | 0           | eliminated    |
| `revl_push` elements copied (O(n^2))    | 5,912,806    | 0           | eliminated    |
| `revl_push` micro-bench (E)             | 320.95 ms    | 0.00 ms     | gone          |
| total heap allocations / pass           | 12,085,346   | 289,177     | 41.8x fewer   |
| total heap bytes / pass                 | 1,249.3 MB   | 227.7 MB    | 5.5x less     |

The `bench_selfhost_rust.py` harness agrees on its own timing path: the lexer
goes from 0.06x to 0.8x CPython (26.7 ms). The O(tokens^2) accumulator copy is
gone — `revl_push` clone-on-append drops to zero calls and allocations fall 42x,
so the run is no longer allocation-bound. The residual is exactly what item 283
predicted would surface next: the full-source `String::clone` threaded to the
scan helpers is now 97.4% of the remaining heap bytes (item 282's
interprocedural `&[char]` view), and the O(i) `charAt` front-walk (~9.6 ms) is
the largest remaining time component (items 276/282). Neither is the accumulator
copy; item 284 closes that one. Reproduce with
`python3 tools/profile_selfhost_rust.py` and `python3 tools/bench_selfhost_rust.py`.

### Reading for item 231a

Item 231a asks whether the lexer's residual py-tier overhead (4.9x → 4.4x after
item 233's inline char classification) is a **py-only lever** or one a native
tier would erase anyway. With the lexer now measured on the native tier (348.8 ms
vs 20.55 ms CPython), the early read is the opposite of the hoped-for erasure:
the native lexer is ~17x *slower*, because the per-access string cost the py tier
hides (O(1) `s[i]`) is O(n) per access on a Rust `String` (`chars().nth`), which
makes the positional scan O(n^2) (item 277). So the native tier does not erase
the lexer's per-byte tax for free; it exposes a rust-specific one. Until a native
`charAt` is genuinely O(1) end to end (the interprocedural view fix item 277
scopes out) and the slice/value-layer tax (item 276) is addressed, the native
lexer is not yet a fair "after" for the py-tier 4.4x.

## Reproducing

```
python3 tools/bench_selfhost.py        # CPython py-tier baseline (item 229)
python3 tools/bench_selfhost_rust.py   # native rust tier (item 266)
python3 tools/profile_selfhost_rust.py # lexer profile: where the 17x goes (item 283)
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

`profile_selfhost_rust.py` (item 283) re-emits the item-266 lexer crate and runs
the build-profile sweep, the instrumented per-operation decomposition, and the
per-operation micro-benchmarks tabulated in the item-283 section above. It reuses
`bench_selfhost_rust`'s stage/corpus wiring and the same `backends/rust/emit.py`,
needs the same cargo + cordis-rs gate, and runs without sudo. The
allocation/operation counts come from a counting global allocator and atomic
counters injected into the emitted lexer by the tool (it never edits the backend).
The leaf-level `malloc`/`memmove`/`advance_by` split quoted above is from
`/usr/bin/sample` on a longer-running build of the same binary.
