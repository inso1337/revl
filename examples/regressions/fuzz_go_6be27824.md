# Cross-tier divergence: `go` disagrees with the `py` reference

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the empirical counterpart to item 133), seed 2, program #5.
- Property under test: one composition has the same meaning across tiers. The shared frontend ADMITTED this program (it compiles), the `py` reference tier ran it, and the `go` tier built and ran the admitted program but computed a DIFFERENT value than the reference (a silent cross-tier value divergence).
- Divergence kind: **value**
- Root-cause fingerprint: `assertion failed: revlEq(probe(), []any{})`
- Maps to roadmap item: 280 (go Opt / empty-list / wildcard-match gaps)

## Per-tier outcome

- `py` (reference): pass — reference passed
- `go`: fail — go test exited 1

### `go` output

```
--- FAIL: TestCrossTierProbe (0.00s)
    gen_test.go:18: assertion failed: revlEq(probe(), []any{})
FAIL
FAIL	revltest	0.440s
FAIL
```

## Minimized program (`fuzz_go_6be27824.rvl`)

```revl
pub fn probe() -> List[Str] { return [] }

test "cross_tier_probe" { assert probe() == [] }
```
