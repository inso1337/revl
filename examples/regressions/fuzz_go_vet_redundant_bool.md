# Cross-tier divergence: `go` disagrees with the `py` reference (NEW)

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292), then minimized by hand.
- Divergence kind: **build** (the go tier cannot validate the emitted program).
- Tiers that disagree: `py` (reference) runs and returns `false`; `go` fails the
  build because `go test` runs `go vet`, which rejects the emitted expression.
- Maps to roadmap item: **NEW** — not 280 / 302 / 304. This is a go test-harness
  strictness gap, orthogonal to those value/resolve fixes.

## Property under test

One composition has the same meaning across tiers. The frontend ADMITTED this
program and the `py` reference ran it; the `go` tier could not build/validate it.

## Minimized program (`fuzz_go_vet_redundant_bool.rvl`)

```revl
pub fn probe() -> Bool { return (false || false) }
test "cross_tier_probe" { assert probe() == false }
```

## Per-tier outcome

- `py` (reference): pass — returns `false`.
- `go`: fail — `go test exited 1`:

```
FAIL	revltest [build failed]
FAIL
# revltest
# [revltest]
./gen_test.go:10:10: redundant or: false || false
```

## Root cause

revl admits any boolean expression with two identical operands
(`a || a`, `a && a`) and the `py` runtime evaluates it. The go tier runs the
emitted test with `go test ./...`, which invokes `go vet` by default. vet's
`bools` analyzer flags identical-operand boolean ops as `redundant or` /
`redundant and` and returns non-zero, so an admitted program that runs
everywhere else fails to "build" on go.

Confirmed boundary (all fail on go, pass on py):
`(false || false)`, `(true || true)`, `let x=true; (x || x)`,
`let x=true; (x && x)`. A non-redundant op like `(true || false)` passes.

- Trigger: any emitted `a || a` or `a && a` with syntactically identical operands.
- Backend file: `src/revl/test.py`, the go runner (`go test ./...`, ~L374). The
  cross-tier contract is "the emitter's output runs"; running the differential
  test suite under `go vet` couples it to a linter stricter than the Go compiler.
  Fix options: run the go tier with `-vet=off`, or have `backends/go/emit.py`
  fold constant/identical-operand boolean ops. Recorded here so the human picks.
