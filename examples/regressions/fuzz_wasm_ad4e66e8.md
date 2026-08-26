# Cross-tier divergence: `wasm` disagrees with the `py` reference

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the empirical counterpart to item 133), seed 2, program #7.
- Property under test: one composition has the same meaning across tiers. The shared frontend ADMITTED this program (it compiles), the `py` reference tier ran it, and the `wasm` tier could not build/validate the code its own emitter produced for the admitted program (a compiles-implies-runs violation).
- Divergence kind: **build**
- Root-cause fingerprint: `Invalid input WebAssembly code at offset N: type mismatch: expected i32, found f64`
- Maps to roadmap item: NEW — not one of 278/279/280; please file

## Per-tier outcome

- `py` (reference): pass — reference passed
- `wasm`: fail — 1 of 1 test(s) failed

### `wasm` output

```
FAIL cross_tier_probe:     1: Invalid input WebAssembly code at offset 2165: type mismatch: expected i32, found f64
```

## Minimized program (`fuzz_wasm_ad4e66e8.rvl`)

```revl
pub fn probe() -> Int { return [0.0, 0.0, 0.0].length() }

test "cross_tier_probe" { assert probe() == 3 }
```
