# findings — harness milestone 6 (agent/harness-m3, second wave)

Self-hosting admission gate (the roadmap's "glitch agents" lighthouse
shape) + multi-session UI. Harness repo: `~/Projects/revl-harness`,
commits `ad4403a` / `605b88e`.

## 1. Refusal log

- **`store.keys()` on a host `Map.new()` stub compiles, crashes at
  runtime** —
  ```revl
  let store = effect Map.new() undo store.drop()
  provide sessions { fn list() = store.keys() }
  ```
  compiles (the checker routes `keys` to the stdlib builtin
  `("keys", ("Map", [], "List[Str]"))`), but the py emitter lowers it as
  `sorted(store)` where `store` is the host `Map` *object*
  (backends/python/runtime.py:1106) — `AttributeError: 'Map' object has
  no attribute 'keys'` at boot. Verdict: **`gap` (py emitter)** — the
  host Map family surface (new/insert/remove/get/drop) never gained
  iteration, yet the builtin table accepts `keys`/`size` on it. Filed as
  roadmap item 84. Workaround shipped: a `__ids__` list stored inside the
  same host Map, joined to the same effect accumulator so it reverts on
  unload.
- **No `if` in provide bodies (again)** — the web dispatch wanted four
  branches; G6 allows none. Verdict: `friction` (known since milestone 1)
  — the fix pattern is now automatic (ternary chain, `emit` as
  expression), but it is the single most-encountered friction in the
  whole harness. Every new author hits it.

## 2. Friction log

- `[slow]` **revl strings have no escape sequences** — `"a\nb"` is
  literal backslash-n, so multi-line component sources in tests had to be
  rewritten as single-line strings (the admission tests pass the proposed
  component as one line). For a *self-hosting* harness this is the wrong
  shape: the agent will want to emit real multi-line `.rvl` source.
  A `"""..."""` triple-quoted form (or documented concat idiom) would
  matter here. Not filed yet — worth a roadmap item (FR-19 candidate).
- `[nit]` **`revl audit` output is not quite JSON for `--json` with
  multiple components** — the audit saved fine; no issue this wave.

## 3. What revl gave us (this wave)

- **The self-hosting loop is a one-liner in revl.** The admission gate is
  a single emission extern; the agent loop calls it through the tool
  registry like any other tool; the compiler's refusal is the tool's
  return value. The roadmap's "agents author components, the compiler is
  the admission gate" shape took ~30 lines of revl.
- **The G8 surface told the truth.** `revl audit` on the self-hosting
  composition shows exactly one crossing (`revl_compile`, emission) —
  the whole "harness can run the compiler" capability, enumerated.
- **`Map` value-typing is genuinely untyped on host objects** — storing a
  `List[Str]` index next to `List[Msg]` values in the same host Map
  compiled and ran; the workaround was legal, not a hack.

## 4. Time-to-green

- Compose → refuse → fix cycles: **3** (Map.keys runtime crash; no-if in
  dispatch — twice; escape-sequence test-source rewrite).
- Longest stall: **the Map.keys runtime crash** — the checker said yes,
  the compiler said yes, the runtime said no; ~3 probes to isolate
  (host-family surface vs builtin table). Item 84's fix (or the harness
  workaround note) cuts this to zero.

## 5. Cost ledger

- `missing-feature` — host-Map iteration (item 84); multi-line string
  literals for self-hosting (FR-19 candidate).
- `diagnostic` — none this wave; the crash was loud (AttributeError) even
  if late.
- `tooling` — none.

**Single change that would cut the most cost next:** item 84 (host-Map
`keys`/`size`). It unblocks the natural session-index spelling, and its
companion (a `--validate`-style compile check, item 78 residual) would
have made it a red compile instead of a boot-time crash.
