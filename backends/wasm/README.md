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

`await Job.run(Int)` is supported (A1): the segment launches an async
host op, the runtime awaits it before the next boundary check — the
iteration lands (inertia is physical), a divert during the wait skips
every later step, and a refusing job is L-Raise with paper-faithful
withholding. See demo.py scenarios 6-8.

## Tier restrictions (all hard EmitErrors, never silent)

| not lowerable | why | where it lives instead |
|---|---|---|
| `config` blocks | no instantiation-config channel yet | hosted backends |
| host builtins (`Pool`, `Map`; `Job` outside `await`) | different host namespace | express state through coeffects |
| method-time effects | the accumulator is fixed at activation | hosted backends |
| non-Int component services | component tier is i32-only | hosted backends / WIT tier |
| variant values + `match` in v3 fns | no tagged unions in core Wasm | documented layout comments |
| `indexOf` builtin | not lowered yet | hosted backends |

## Now supported (v2 + typed v3)

- **ir v2 realms/intercept**: `isolate` becomes the import/export namespace
  (`coeffect:tenant_a/kv` / `provide:tenant_a/kv.<op>`), and metadata is
  carried in `revl:isolate` / `revl:intercept` custom sections. Intercept
  enforcement remains host-side (advisory on this tier).
- **ir v3 Str/List/record values**: emitted in a linear-memory
  canonical-ABI-shaped representation (`u32` length/count prefix); the
  module exports `memory` so a host can read results. `Int`/`Bool` remain
  plain i32. Supported builtins: `length`, `push`, `concat`, `slice`,
  `charAt`, `charCodeAt`.
- **`await Job.run(Int)`** continues to lower to the runtime's async host op.

## Notable semantics on this tier

The runtime follows the **paper's** failure semantics (L-Raise = trap,
failed fibers withheld until explicit `retry`) — the very divergence
cordis-py marks as a strict xfail. Compiled revl inherits it: this backend
is the most calculus-faithful of the three.
