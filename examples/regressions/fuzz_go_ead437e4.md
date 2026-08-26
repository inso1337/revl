# Cross-tier divergence: `go` disagrees with the `py` reference

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the empirical counterpart to item 133), seed 2, program #0.
- Property under test: one composition has the same meaning across tiers. The shared frontend ADMITTED this program (it compiles), the `py` reference tier ran it, and the `go` tier could not build/validate the code its own emitter produced for the admitted program (a compiles-implies-runs violation).
- Divergence kind: **build**
- Root-cause fingerprint: `s (variable of struct type Adt0C0_0) is not an interface`
- Maps to roadmap item: 280 (go Opt / empty-list / wildcard-match gaps)

## Per-tier outcome

- `py` (reference): pass — reference passed
- `go`: fail — go test exited 1

### `go` output

```
FAIL	revltest [build failed]
FAIL
# revltest [revltest.test]
./gen_test.go:22:15: s (variable of struct type Adt0C0_0) is not an interface
./gen_test.go:27:3: declared and not used: _v
./gen_test.go:30:3: declared and not used: _v
```

## Minimized program (`fuzz_go_ead437e4.rvl`)

```revl
type Adt0 = C0_0 | C0_1(Str) | C0_2(Str)
fn use_Adt0() -> Int { let s = C0_0
  return match s { C0_0 => 0, C0_1(_v) => 1, C0_2(_v) => 2 } }
pub fn probe() -> Int { return 0 }

test "cross_tier_probe" { assert probe() == 0 }
```
