# revl → cordis-wasm backend (substrate tier)

Compiles revl components to **WAT modules** for the
[cordis-wasm](https://github.com/inso1337) runtime prototype
(`~/Projects/cordis-wasm`), where the paradigm is enforced by the sandbox:

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

## Tier restrictions (all hard EmitErrors, never silent)

| not lowerable | why | where it lives instead |
|---|---|---|
| strings / `format` | i32-only op signatures | hosted backends |
| `config` blocks | no instantiation-config channel yet | hosted backends |
| host builtins (`Pool`, `Map`, `Job`) | different host namespace | express state through coeffects |
| `await` steps | cordis-wasm implements the sync base calculus | hosted backends, or extend the runtime |
| method-time effects | the accumulator is fixed at activation | hosted backends |

## Notable semantics on this tier

The runtime follows the **paper's** failure semantics (L-Raise = trap,
failed fibers withheld until explicit `retry`) — the very divergence
cordis-py marks as a strict xfail. Compiled revl inherits it: this backend
is the most calculus-faithful of the three.
