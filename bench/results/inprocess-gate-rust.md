# In-process admission gate (item 333, Slice 2, rust)

An agent tool-generation loop written in rust links the revl gate as a LIBRARY
and screens every component it proposes IN ITS OWN PROCESS - no `revl mcp
serve`, no IPC, no wire, and no Python anywhere. This file records what that
buys and, just as load-bearing, what it does not. Produced by
`bench/inprocess_gate_rust` (`cargo run --release --manifest-path
bench/inprocess_gate_rust/Cargo.toml -- --write`).

Gate surface: `api=1.0.0`, `language=2.0.0`, `frontier=selfhost-admit:019e01b8fb177e01`.
Layer decided: composition + guarantee layer (G1..G4, A1, PRELUDE) and parse (BAD); NOT the reference type layer.

## This gate issues no admissions - read this before wiring it in

The py harness (`bench/results/inprocess-gate.md`) proves an IDENTITY:
`revl.gate.admit` IS the reference admission path, so the in-process verdict IS
the reference verdict. **This harness cannot and does not claim that.**
`revl-gate` is the self-host front end compiled to rust; it decides the
composition/guarantee layer and runs NO type layer, so it has no admission arm
at all. Its three verdicts are `refused`, `no_objection` and `outside_frontier`,
and the wire reports `"admitted": false` on every one of them.

What the rust embed buys is the other direction: a local, Python-free REFUSAL
that agrees with the reference compiler on the covered corpus. A refusal is
worth acting on. A no-objection is NOT an admission - before running anything,
get a reference verdict (`revl compile`, or `revl.gate.admit` on py).

## The manifest arm (issue #346): half of it closed here

`revl_gate::admit_into(source, manifest)` is real as of issue #346: it folds
G2/G3 over the UNION of the running composition's rows and the candidate, so a
rust embed can ask the py loop's admitting question locally. It is a REFUSAL
arm, not an admission arm - it still reports `"admitted": false`, and it refuses
what the py gate refuses, with the same code and the same why-trace.

What that closes and what it does not, against `held_manifest` =
`Kv/store/;App/app/;App<store` (the py harness's `base_manifest()` wire):

* **closed** - the ambient half of G2/G3: a candidate whose provides collide
  with a RUNNING key is refused. `ambient_provision_conflict` below is the exit
  case: py's `revl.gate.admit_into` says `G2`, this gate says `G2` with the same
  sentence, and py's STANDALONE `admit` ADMITS the same bytes - so the manifest
  is demonstrably being read, not ignored.
* **still open** - requirement RESOLUTION. py's `admit_into` resolves a
  candidate's `requires` against the running providers; the self-host fold only
  checks disjointness, acyclicity and route realms, so it cannot. `cache_layer`
  is that half made concrete: py ADMITS it into the running manifest, this gate
  can only decline to object. Closing it is the self-host TYPE LAYER's job, its
  own roadmap lane, and is deliberately NOT attempted here.

## The batch, screened in-process

| candidate | standalone | into the held manifest | shared with the py harness | note |
|---|---|---|---|---|
| `standalone_twin` | no objection | not asked | yes | standalone-valid, Store inlined; py ADMITS it |
| `cache_layer` | no objection | no objection | yes | requires a Store not in the source; py refuses it standalone and ADMITS it into the running manifest |
| `ambient_provision_conflict` | no objection | refuse (G2) | no | provides a key the running composition already provides; py refuses it into the manifest (G2) and ADMITS it standalone |
| `incomplete_provide` | no objection | not asked | yes | provides a service but omits a declared method; py refuses |
| `provision_conflict` | refuse (G2) | not asked | no | two components provide the same service; py refuses (G2) |
| `undeclared_emission` | refuse (G4) | not asked | no | an undeclared emission is called from a body; py refuses (G4) |
| `syntax_error` | no objection | not asked | yes | a genuine parse failure; py refuses |
| `hole_draft` | no objection | not asked | yes | a draft with an open typed hole; py refuses (T3) |
| `type_layer_miss` | no objection | not asked | no | a type error; py refuses (T1) |
| `frontier_oversized` | declined (FRONTIER) | not asked | no | a source over the size bound; py ADMITS, this gate is not entitled to decide |

2 refused, 7 no-objection, 1 declined. Every one of them
serialises as `"admitted": false`; nothing in this batch produced anything a
host could read as an admission, and every refusal it did issue is a refusal the
py admission gate also issues, with the same guarantee tag
(`tests/test_inprocess_gate_rust.py`).

Verdicts are order-independent: screening the batch in a fixed order and in a
shuffled order in the same process yields identical per-candidate verdicts on
BOTH arms (holds), which is the property that proves the gate is stateless.

### What the screen catches, measured

Seven of these candidates are ones the py admission gate REFUSES. This gate
refuses **two** of them: the `G2` provision conflict and the `G4` undeclared
emission. The other five come back as no-objections:

* `cache_layer` - a `requires store: Store` that resolves to nothing (py refuses
  it standalone, and ADMITS it into the running composition);
* `incomplete_provide` - a `provide` block missing a declared method;
* `syntax_error` - a source the reference parser rejects outright. The crate
  documents parse failures as `BAD`, and that arm is real (`@@@ not revl @@@`
  gets it), but the self-host parser is more permissive than the reference and
  this program walks through it;
* `hole_draft` - a draft with an open typed hole (py refuses `T3`);
* `type_layer_miss` - a return-type mismatch (py refuses `T1`).

Every one of those five is in the TOLERATED direction: a no-objection is never
an admission, so none of them is a false admit. Together they are the reason the
crate has no `Admitted` arm, and the reason an embedder that reads a
no-objection as a green ships an unsafe host. The one candidate this gate
declines outright (`frontier_oversized`) is the fail-closed path working: `py`
ADMITS it, and rather than decide a construct it does not cover, the gate says
so.

### The `admit_into` gap, priced - and one leg of it closed

Before #346 the realistic agent shape - admit a candidate AGAINST the running
composition - was not available on rust at all. It now is for the ambient half:
`ambient_provision_conflict` is a candidate that is HARMLESS standalone (py
ADMITS the same bytes) and is refused `G2` once the running composition is in
the fold, on both arms, with the same why-trace. That is the half that closed.

The other half is what `cache_layer` still prices. py's `admit_into` ADMITS it
into the running manifest - it RESOLVES the `requires store: Store` against the
running `Kv` provider - and the only thing this gate can say about it, with or
without the manifest, is that it does not object. Requirement resolution is the
self-host type layer's job, and this file is where the remaining distance is
measured, not smoothed over.

## Fail closed

* an oversized source (over `MAX_SOURCE_BYTES` = 262144) is DECLINED
  (holds) rather than risked: the emitted front end is deeply recursive and
  a stack exhaustion aborts, which cannot be turned back into a refusal;
* the same source, as a batch CANDIDATE (`frontier_oversized` above), is declined
  with code `FRONTIER` — the batch's fail-closed arm. The generated frontier
  table's two LEXICAL rows (excluded keywords, excluded builtins) are both empty
  at this generation: item 391 ported the last builtins they named, so the size
  bound is the trigger a batch can still demonstrate;
* `compile_to` refuses on both tiers (holds) - the self-host emitters
  still carry `@py`-only helper externs, so there is no native emitter to call
  (item 332 Stage 4).

## Cost: milliseconds, and super-linear in candidate size

The timed section is one `revl_gate::admit` call on an in-memory string: no disk
I/O, no network, no toolchain, no Python, no process hop. Nothing else is in it.

| candidate size | bytes | median (ms) | p90 (ms) | p99 (ms) | samples |
|---|---|---|---|---|---|
| small (3 methods) | 218 | 0.711 | 1.348 | 7.802 | 25 |
| medium (12 methods) | 636 | 5.183 | 9.193 | 11.694 | 25 |
| large (48 methods) | 2364 | 71.082 | 107.075 | 224.205 | 25 |

The representative scenario (the py harness's `standalone_twin`, 276 B)
measured median **0.754 ms** (p90 0.807 ms, p99 0.871 ms,
n=25).

**This does not inherit the py headline, and it must not be reported as if it
did.** The py in-process round-trip is tenths of a millisecond and grows roughly
with candidate size. This one starts in the milliseconds and grows far faster
than the source does: 10.8x the bytes costs 100x the time
across the size cells, which is quadratic-shaped, not linear. At a few kilobytes
- an ordinary model-authored component - a single screen costs on the order of a
second. An agent loop that screens every candidate inline would feel that.

### Where the cost lives

Three candidates of roughly equal BYTE length and very different token and
declaration counts, timed the same way. If the cost tracked source bytes it
would be flat across these rows; it is not.

| shape | bytes | verdict | median (ms) | samples |
|---|---|---|---|---|
| declaration-heavy | 1212 | no_objection | 16.014 | 6 |
| statement-heavy | 1214 | no_objection | 15.327 | 6 |
| comment-padded | 1248 | no_objection | 0.709 | 6 |

The comment-padded shape - the same byte count, a fraction of the tokens - is
roughly 23x cheaper than the declaration-heavy one, while the
statement-heavy shape, which carries ONE declaration and a body full of
statements, costs the same order as the declaration-heavy one. So the cost
tracks TOKENS: it lives in the emitted lexer/parser, not in the composition gate
walking declarations, and not in raw source bytes. That is an attribution, not a
fix - the fix belongs to whoever owns the emitted self-host front end's
algorithmic shape (items 391/336), and this file is the measurement they should
start from.

### Methodology, and how much to trust the absolute figures

- **Build:** release. A debug build's numbers are a fiction, so the harness
  documents the release invocation and nothing else.
- **Platform:** macos/aarch64. Sampling: `--iters 25` per size cell with
  `--warmup 3`, and a quarter of that per shape probe (the shape question
  is orders of magnitude, not percent).
- **Timed section:** one `revl_gate::admit` call on an in-memory string.
- **Variance:** these medians move by a FACTOR of a few between runs on a shared
  machine - the p90/p99 columns show it. So read the SHAPE of the result, which
  is stable and is the finding: milliseconds at the floor, super-linear in
  candidate size, token-bound. Do not quote a single figure from this table as
  "the rust gate's latency".

## The honest boundary

This is a COMPILE-TIME screen, not a sandbox. A component this gate refuses
never runs in the embedder's process. A component it does not refuse has been
screened at the composition/guarantee layer ONLY: not type-checked, not
admitted, not confined. `admitted` is not something this gate issues - not
standalone and not from the manifest arm, which is a refusal arm that reads the
running composition, not an admission into it. And even a py admission is not
"safe to run unwitnessed" - the reversible-run half is item 334.

## Re-run

```
cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml
cargo run --release --manifest-path bench/inprocess_gate_rust/Cargo.toml -- --write
```

Timings above come from `--iters 25 --warmup 3` in a release build; a
debug build's numbers are a fiction and the harness says so rather than
publishing them.

A guard test, `tests/test_inprocess_gate_rust.py`, builds and runs this harness
in CI (the `backend-rust` job), re-derives the PY verdict for each candidate's
exact source bytes - both standalone and against the held manifest - and holds
the two harnesses against each other: every rust refusal must be a real py
refusal with the same code, every rust `admit_into` refusal must be a real
`revl.gate.admit_into` refusal with the same code AND the same why-trace, no arm
may read as an admission, the measured layer gap must still be a gap, and the py
harness's own candidates must still be the bytes screened here.
