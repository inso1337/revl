# Codegen performance harness: the go backend

What the go emitter *generates*, measured against the Go a competent Go
developer would write by hand for the same semantics. The gap is emitter
waste, and this directory is the repeatable way to see it.

Filed as **docs/v2.0-roadmap.md item 433**.

## Run it

```
cd bench/codegen/go
go test ./ab/ -run Test                          # the hand side is still the same program
go test ./ab/ -bench . -benchmem -benchtime=2000x -run XXX   # the A/B table
go build -gcflags='-m' ./emitted/loops/          # escape-analysis evidence
```

No external dependencies. The ir_version 3 pure tier the harness targets is
ordinary Go, so nothing links against stc-go and the run is offline.

## Read allocs/op and B/op. Nothing else.

Item 433 reports **no timings at all**, on purpose. The audit ran on a machine
with a dozen concurrent agents, where even an interleaved A/B ratio samples
two different load conditions. `allocs/op` and `B/op` are exact, reproducible
and load-independent, so every finding is written against those, against
`go build -gcflags='-m'` escape analysis, or against a complexity argument.

A later timing pass on a quiet machine is the same command with a larger
`-benchtime`. Nothing in this directory needs to change for it.

Three kinds of evidence live here, in descending strength:

1. **Allocation counters.** `allocs/op` and `B/op` for the emitted lowering
   against the hand-written one.
2. **Size-scaled pairs.** `Collect`, `Tally`, `Build` and `Scan` each run at
   two input sizes 10x apart. `B/op` growing ~100x for a 10x input is a
   quadratic, whatever the clock says, and the hand side staying flat at one
   allocation is the contrast.
3. **In-language controls.** `BenchmarkTagEmitted` and
   `BenchmarkTagJoinedEmitted` run `${a}/${b}` and `a + "/" + b` through the
   SAME emitter. Nothing varies but the lowering.

One benchmark-design note worth keeping: the interpolation benchmarks draw
their arguments from a slice, not from literals. With literal arguments the
compiler inlines the call and folds the boxed interface operands into
read-only static data, and the boxing vanishes from the counters.

## Layout

| path | what it is |
|---|---|
| `probe.rvl`, `probe2.rvl` | the benchmark programs, in revl |
| `probe.ir.json`, `probe2.ir.json` | their compiled IR, checked in so the bench runs without the frontend |
| `emitted/loops/gen.go`, `emitted/values/gen.go` | `backends/go/emit.py` output, verbatim |
| `emitted/*/exports.go` | hand-written wrappers. The emitter lowers a revl `pub fn` to an unexported Go func, so a benchmark in another package cannot call it |
| `hand/hand.go` | the yardstick |
| `ab/ab_test.go` | the A/B benchmarks, and the equivalence tests that make them mean anything |

`regen.sh` recompiles the IR and re-emits the Go after an emitter change.

## The rules the yardstick holds itself to

Stated in the `hand` package doc comment, repeated here because they are what
makes a measured gap a finding rather than an artifact:

- Same observable semantics, revl's checked `Int` arithmetic included. The
  hand side carries `revlAdd`'s overflow panic verbatim, so no gap is ever
  just "the hand version dropped a safety check".
- Same code-point (not byte) `Str` indexing, per `docs/strings.md`.
- Same persistence *where the revl program observes it*. In the `out =
  out.push(i)` and `m = m.set(k, v)` loops the old value is dead on the next
  line, so the hand side mutates in place. That is both what a Go developer
  writes and what an emitter with a liveness check could emit.
- No algorithm substitution. `hand.IndexOf` still answers a code-point index;
  `hand.Take` clamps exactly as `revlStrSlice` does.

The `Test*` functions in `ab_test.go` enforce all of this. If one fails, the
hand side is no longer the same program and its numbers are worthless.

## Controls

`SumIds`, `Bucket`, `Describe` and `Find` all report 0 allocs/op on both
sides. They are in the suite so a regression in the *harness* shows up as a
control moving, not as a finding quietly getting better, and because the
negative results are as much of the audit as the findings.
