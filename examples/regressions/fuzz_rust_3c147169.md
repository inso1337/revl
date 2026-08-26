# Cross-tier divergence: `rust` disagrees with the `py` reference

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292 — the empirical counterpart to item 133), seed 5, program #0.
- Property under test: one composition has the same meaning across tiers. The shared frontend ADMITTED this program (it compiles), the `py` reference tier ran it, and the `rust` tier could not build/validate the code its own emitter produced for the admitted program (a compiles-implies-runs violation).
- Divergence kind: **build**
- Root-cause fingerprint: `error[E0283]: type annotations needed`
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
   Compiling quote v1.0.47
   Compiling unicode-ident v1.0.24
   Compiling serde_core v1.0.229
   Compiling zmij v1.0.23
   Compiling serde_json v1.0.151
   Compiling serde v1.0.229
   Compiling itoa v1.0.18
   Compiling memchr v2.8.3
   Compiling cordis-rs v0.3.0
   Compiling syn v3.0.4
   Compiling serde_derive v1.0.229
   Compiling revl_test v0.1.0 (/private/var/folders/fh/n66jb8v55qx34nrwhp4mckkm0000gp/T/revl_test_rust_pvmhx85h)
error[E0283]: type annotations needed
  --> src/lib.rs:9:29
   |
9  |     return (0i64 < (vec![]).revl_length());
   |                             ^^^^^^^^^^^ cannot infer type for type parameter `T`
   |
   = note: cannot satisfy `_: Clone`
note: required for `Vec<_>` to implement `RevlListOps<_>`
  --> src/lib.rs:67:28
   |
67 | impl<T: Clone + PartialEq> RevlListOps<T> for Vec<T> {
   |         -----              ^^^^^^^^^^^^^^     ^^^^^^
   |         |
   |         unsatisfied trait bound introduced here

For more information about this error, try `rustc --explain E0283`.
error: could not compile `revl_test` (lib) due to 1 previous error
warning: build failed, waiting for other jobs to finish...
error: could not compile `revl_test` (lib test) due to 1 previous error
```

## Minimized program (`fuzz_rust_3c147169.rvl`)

```revl
pub fn probe() -> Bool { return (0 < [].length()) }
test "cross_tier_probe" { assert probe() == false }
```
