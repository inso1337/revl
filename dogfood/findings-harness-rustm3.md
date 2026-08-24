# findings — harness multi-tier rust boot (agent/harness-m3, seventh wave)

## 1. Refusal log

- **rust `--once` fails cargo build with 7 errors on the mtier harness** —
  item 91 landed and the fn-type refusal is gone, but the emitted rust has
  three distinct compile bugs (finding #22, roadmap item 93):
  (a) `config` not in scope in provide-method bodies (E0425);
  (b) persistent `current = current.revl_push(...)` moves the value
  (E0382); (c) non-Copy `dec`/`impl Fn` params used twice (E0382).
  Verdict: **`gap` (rust emitter)** — all three are local to
  backends/rust/emit.py, all have minimal repros in the mtier harness.
  The fn-type lowering (item 91) exposed the next layer: the emitted
  bodies around it were never exercised with config-in-provide + loops +
  fn-typed callbacks on rust.

## 2. Friction log

- `[slow]` **A minimal config-in-provide probe did not reproduce** — the
  `_emitter("rust").emit(ir)` path emits only the service interfaces for a
  v3 document; the component bodies live in the run driver's
  `_emit_component_auto`. The mtier full-composition `revl run` is the
  authoritative repro. Noted for future probes: use `revl run --backend
  rust` (or the driver's emit path), not the bare emitter.

## 3. What revl gave us

- **Item 91 is real**: `impl Fn` params emit (the fn-type refusal is
  gone), and the remaining failures are ordinary rust compile errors in
  emitted bodies — a strict improvement over a hard refusal, and exactly
  what `--once` (boot -> teardown -> no-residue) is meant to gate.

## 4. Time-to-green

- 1 probe cycle to locate the three bugs from the 7 cargo errors (each
  error mapped to a distinct emitted construct). The emitted-code
  inspection was direct.

## 5. Cost ledger

- `missing-feature` — item 93 (three rust-emitter bugs).
- `tooling` — the bare-emitter vs run-driver emit path mismatch cost one
  probe (see friction log).

**Single change that would cut the most cost next:** item 93(a) — thread
`config` into rust provide-method scope; the other two (b, c) are
clone/borrow fixes in the same emitter. With 93 done, the string-protocol
harness boots on rust and the "runs on all runtimes" claim closes for the
loop shape.
