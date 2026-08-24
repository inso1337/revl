# revl → cordis-wasm backend (substrate tier)

Compiles revl components to **WAT modules** for the
[cordis-wasm](https://github.com/inso1337/cordis-wasm) runtime prototype
, where the paradigm is enforced by the sandbox:

- the coeffect specification **is** the emitted import section
  (`coeffect:<key>` per required service op) — G1/G6 stop being checker
  claims and become the instruction set;
- provisions **are** `provide:<key>.<op>` exports, staged by the runtime and
  published at L-Finish;
- `effect … undo …` pairs compile into an `activate_step() -> i32` state
  machine plus a step-guarded `deactivate()` — the paper's §6.7 compiler-
  emitted accumulator, in Wasm globals. Partial rollback after a divert or
  trap reverts exactly the completed steps' inverses, with zero host
  bookkeeping.

```bash
# emit WAT from an IR document
python3 emit.py <ir.json> <out-dir>

# the end-to-end demo (compiled Beacon + Auditor composing with a
# hand-written WAT kv provider — a polyglot mesh)
~/Projects/cordis-wasm/.venv/bin/python demo.py
```

`await Job.run(name)` is supported (A1): the segment launches an async
host op, the runtime awaits it before the next boundary check — the
iteration lands (inertia is physical), a divert during the wait skips
every later step, and a refusing job is L-Raise with paper-faithful
withholding. See demo.py scenarios 6-8.

## Tier restrictions (all hard EmitErrors, never silent)

The full matrix — which values, which string builtins, and which service
boundary shapes the substrate carries, with the exact refusal each emits — is
**docs/wasm-capabilities.md**. The headline rows:

| not lowerable | why | where it lives instead |
|---|---|---|
| `config` blocks | no instantiation-config channel yet | hosted backends |
| host builtins (`Pool`, `Map`; `Job` outside `await`) | different host namespace | express state through coeffects |
| method-time effects / compensation | the accumulator is fixed at activation | hosted backends |
| `Float` / `Map` / function-typed values (any position) | no value representation in the canonical-ABI model (Float has only the interpolation subset) | hosted backends / WIT tier |
| `repeat` builtin, `List.indexOf` | `repeat` and the List per-element search not lowered yet (`Str.split`/`join`/`indexOf` now DO lower — the reader trio, `$str_split`/`$str_join`/`$str_index_of`) | hosted backends |
| non-scalar instance-accessor payloads (`spawn`) | a pointer would cross into memory the spawner does not own | hosted backends |

## Now supported (v2 + typed v3)

- **ir v2 realms/intercept**: `isolate` becomes the import/export namespace
  (`coeffect:tenant_a/kv` / `provide:tenant_a/kv.<op>`), and metadata is
  carried in `revl:isolate` / `revl:intercept` custom sections. Intercept
  enforcement remains host-side (advisory on this tier).
- **ir v3 Str/List/record/variant values**: emitted in a linear-memory
  canonical-ABI-shaped representation (`u32` length/count prefix, then one
  8-byte slot per field/element); the module exports `memory` so a host can
  read results. Supported builtins: `length`, `push`, `concat`, `slice`,
  `charAt`, `charCodeAt`, `to_str`, `startsWith`, `endsWith`, `to_int`
  (the Str parse; the Int32 widen is the sign-extend), and the reader trio
  `split` / `join` / `Str.indexOf` (`$str_split` / `$str_join` /
  `$str_index_of`, pulled in on demand) — the full list is the matrix doc,
  docs/wasm-capabilities.md).
- **`match` / variants in component and method bodies**: tagged-union cells
  (`[u32 tag][pad][payload]`), including `match` over `Opt`/`Result`.
- **rich service boundaries**: a Str/List/record/variant/`Opt`/`Result`
  service param or return crosses as a pointer into the module's memory —
  the v3 value model widened the boundary beyond scalars.
- **`await Job.run(name)`** continues to lower to the runtime's async host op.
- **standard WASI Preview 2 component (`Str` boundary)** — `canonical.py`
  (item 41 slice-3). The linear-memory representation above is the tier's *own*
  ABI, read through the exported `memory`; `canonical.py` maps it to the
  **standard canonical ABI** at the component boundary so a Component Model host
  (wasmtime, wasmCloud, Spin, jco) can load it. It adds `cabi_realloc` and a
  canonical export per pure `Str`-only function (bare `(ptr,len)` lift/lower +
  return area), wraps the core module into a component with `wasm-tools
  component embed`/`new`, and the component's exported interface is exactly
  `revl export wit` (slice-1). Records/lists/variants at the canonical boundary
  are the remaining follow-on. See `test_canonical_abi.py` (builds + runs it
  under wasmtime's component model) and docs/wit-bridge.md §5.

## Widths: `Int` is i64, addresses are i32

`Int` is 64-bit two's complement with trapping overflow on every revl tier
(docs/arithmetic.md), and this one is no exception: Int values, Int-typed
params/locals/results/globals, and the coeffect/provision ABI are all `i64`,
and `+`/`-`/`*` go through `$int_add`/`$int_sub`/`$int_mul`, which test for
overflow and execute `unreachable`.

What stays `i32` is everything that is not an Int *value*: every
linear-memory address (wasm32 addressing is 32-bit), `Bool`, the string
byte-length and list-count prefixes, a variant's tag, the `activate_step`
result and `$__step`, loop cursors, and `Job.run`'s interned job id.
`_wasm_ty` in `emit.py` is the single place that decides which a type is.

The one part of the guarantee this tier cannot carry is the *message*: a wasm
trap has no payload, so it faults but does not say `revl: Int overflow` the
way the hosted tiers do. The same already applied to its division-by-zero
fault.

## Notable semantics on this tier

The runtime follows the **paper's** failure semantics (L-Raise = trap,
failed fibers withheld until explicit `retry`) — the very divergence
cordis-py marks as a strict xfail. Compiled revl inherits it: this backend
is the most calculus-faithful of the three.
