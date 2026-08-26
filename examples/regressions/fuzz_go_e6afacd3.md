# Cross-tier divergence: `go` disagrees with the `py` reference

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the empirical counterpart to item 133), seed 2, program #6.
- Property under test: one composition has the same meaning across tiers. The shared frontend ADMITTED this program (it compiles), the `py` reference tier ran it, and the `go` tier could not build/validate the code its own emitter produced for the admitted program (a compiles-implies-runs violation).
- Divergence kind: **build**
- Root-cause fingerprint: `cannot use []any{} (value of type []any) as []int64 value in argument to h0`
- Maps to roadmap item: 280 (go Opt / empty-list / wildcard-match gaps)

## Per-tier outcome

- `py` (reference): pass — reference passed
- `go`: fail — go test exited 1

### `go` output

```
FAIL	revltest [build failed]
FAIL
# revltest [revltest.test]
./gen_test.go:40:41: cannot use []any{} (value of type []any) as []int64 value in argument to h0
```

## Minimized program (`fuzz_go_e6afacd3.rvl`)

```revl
fn h0(p0: Bool, p1: List[Int]) -> Str { return (("" + "") + ("fdkl" + "mbb")) }
fn h1(p0: List[Bool], p1: Opt[Float]) -> List[Str] { return [("" + "xrl"), h0(true, [])] }
fn h2(p0: Int, p1: Str) -> Float { return ((-1.6 / 3.3) - 5.8) }
pub fn probe() -> Float { return 0.0 }

test "cross_tier_probe" { assert probe() == 0.0 }
```
