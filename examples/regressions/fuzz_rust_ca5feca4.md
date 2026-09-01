# Cross-tier divergence: `rust` disagrees with the `py` reference

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the empirical counterpart to item 133), seed 5, program #1.
- Property under test: one composition has the same meaning across tiers. The shared frontend ADMITTED this program (it compiles), the `py` reference tier ran it, and the `rust` tier could not build/validate the code its own emitter produced for the admitted program (a compiles-implies-runs violation).
- Divergence kind: **build**
- Root-cause fingerprint: `error[E0308]: mismatched types`
- Maps to roadmap item: 278 (rust build gaps)

## Per-tier outcome

- `py` (reference): pass — reference passed
- `rust`: fail — cargo test exited 101

### `rust` output

```
Updating crates.io index
     Locking 12 packages to latest compatible versions
      Adding cordis-rs v0.3.0 (available: v0.6.1)
   Compiling proc-macro2 v1.0.107
   Compiling unicode-ident v1.0.24
   Compiling quote v1.0.47
   Compiling serde_core v1.0.229
   Compiling zmij v1.0.23
   Compiling serde v1.0.229
   Compiling serde_json v1.0.151
   Compiling itoa v1.0.18
   Compiling memchr v2.8.3
   Compiling cordis-rs v0.3.0
   Compiling syn v3.0.4
   Compiling serde_derive v1.0.229
   Compiling revl_test v0.1.0 (/private/var/folders/fh/n66jb8v55qx34nrwhp4mckkm0000gp/T/revl_test_rust_0eog6940)
error[E0308]: mismatched types
  --> src/lib.rs:13:26
   |
13 |     return (h2(vec![]) + h2(vec![]));
   |                          ^^^^^^^^^^ expected `&str`, found `String`
   |
help: consider borrowing here
   |
13 |     return (h2(vec![]) + &h2(vec![]));
   |                          +

For more information about this error, try `rustc --explain E0308`.
error: could not compile `revl_test` (lib) due to 1 previous error
warning: build failed, waiting for other jobs to finish...
error: could not compile `revl_test` (lib test) due to 1 previous error
```

## Minimized program (`fuzz_rust_ca5feca4.rvl`)

```revl
fn h2(p0: List[Bool]) -> Str { return (("" + "ihz") + "djlw") }
pub fn probe() -> Str { return (h2([]) + h2([])) }
test "cross_tier_probe" { assert probe() == "ihzdjlwihzdjlw" }
```
