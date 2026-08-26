# The wasm tier's capability matrix

The wasm tier (`backends/wasm/emit.py`) is the **substrate tier**: components
compile to WAT modules that run on the cordis-wasm runtime under wasmtime, where
the paradigm is enforced by the sandbox itself (a coeffect *is* an import, a
provision *is* an export). It is deliberately the strictest tier, so "runs on
all six" means something narrower here than on the hosted tiers — and the
narrowing is **hard `EmitError`s, never silent degradation**: a composition that
uses something the substrate does not carry fails the emit with a named reason.

This document is the matrix: which values, which string/collection builtins,
and which service boundary shapes the substrate supports, and the exact
diagnostic each refusal emits. It is the contract a harness-shaped component is
written against when it must also run on wasm, and it is what the claim "runs on
all six" is precise about. Every row below was read off the emitter's own
refusal sites (and, for the supported rows, off a compiled probe).

## The value model

What a value *is* decides everything else. The substrate's v3 typed-core
carries a **canonical-ABI linear-memory model** (docs/strings.md, the "unit"
and "canonical ABI" sections): a `u32` length/count prefix then one 8-byte slot
per element, exported through the module's `memory` so a host can read results.

| revl value type | wasm representation | crosses a service boundary? |
|---|---|---|
| `Int` | `i64` value (trapping overflow, docs/arithmetic.md) | yes, by value |
| `Int32` | `i32` value (`to_int`/`to_int32` checked conversions) | yes, by value |
| `Bool` | `i32` value | yes, by value |
| `Str`, `Bytes` | `i32` pointer into linear memory | yes, as a pointer |
| `List[T]` | `i32` pointer (count prefix + 8-byte slots) | yes, as a pointer |
| record | `i32` pointer (one 8-byte slot per field) | yes, as a pointer |
| variant / `Opt[T]` / `Result[T, E]` | `i32` pointer to a `[u32 tag][pad][payload]` cell | yes, as a pointer |
| `Unit` (void op) | no slot | yes (absent) |
| `Float` | **no value representation** — only the template-interpolation subset (`$f64_to_str`) that renders integer-valued finites and **traps** on the exponent form / non-integers (docs/strings.md) | no |
| `Map[K, V]` | **no representation** (a persistent map needs a richer value model) | no |
| function types | **no representation** (wasm MVP: no closures/function references) — arrows called where they are bound still lower, inlined at the call site | no |

The gate is `_check_type` in `backends/wasm/emit.py`: it accepts
`Int`/`Int32`/`Bool`/`Str`/`Bytes`/`List`/record/variant/`Opt`/`Result`
(recursively, for payloads and elements) and refuses everything else by name:

```
type 'Float' is not lowerable — this tier supports
Int/Bool/Str/Bytes/List/record/variant/Opt/Result values
```

(`Map[Str, Int]` gets the same message with the type named.)

## String and collection builtins

The fixed-shape stdlib surface (docs/stdlib-2.0.md) lowers over the canonical
ABI model. The substrate's rule: **`length`, `push`, `concat`, `slice`,
`charAt`, `charCodeAt`, plus the reader builtins `split`, `join` and `Str`'s
`indexOf` (and the arithmetic/rendering builtins) lower; only `repeat`, the
`Map` operations, and `List.indexOf` do not.**

| builtin | on | wasm? | notes / exact refusal |
|---|---|---|---|
| `length()` / `.length` | Str, Bytes, List | ✅ | both the method and property forms: Str counts **code points** (`$str_cp_length`); Bytes/List count from the u32 prefix |
| `push(v)` | List | ✅ | persistent, one 8-byte slot |
| `concat(x)` | Str, Bytes, List | ✅ | `$str_concat` / `$list_concat` |
| `slice(a, b)` | Str, Bytes, List | ✅ | Str slices on code-point boundaries; Bytes on byte offsets; List on element offsets |
| `charAt(i)` | Str, Bytes | ✅ | Str: the whole scalar at a code-point index; Bytes: the single byte |
| `charCodeAt(i)` | Str, Bytes | ✅ | Str decodes the UTF-8 scalar; Bytes reads the raw byte |
| `to_str()` | Int | ✅ | `$int_to_str` (renders `Int.MIN` correctly) |
| `to_int()` / `to_int32()` | Int32 / Int | ✅ | checked width conversions |
| `div_trunc` / `div_floor` / `div_euclid` / `mod` | Int | ✅ | named integer ops, same result every tier |
| `checked_div_trunc` / `_floor` / `_euclid` / `checked_mod` | Int | ✅ | total forms: zero divisor → `Err(...)`, no trap |
| `indexOf(v)` | Str | ✅ | `$str_index_of`: byte substring scan, returns the **code-point index** of the match (or `-1`), matching py/ts |
| `indexOf(v)` | List | ❌ | `indexOf is not lowerable on this tier yet for List — the element comparison has no representation here; use a hosted backend` |
| `split(sep)` | Str | ✅ | `$str_split` → `List[Str]`: JS-shape (trailing empties kept, `""` → per-code-point pieces) |
| `join(sep)` | List[Str] | ✅ | `$str_join`: elements joined by `sep` over the bump heap; `[]` → `""` |
| `repeat(n)` | Str | ❌ | `unsupported builtin method 'repeat'` |
| `set` / `lookup` / `has` / `size` / `keys` / `remove` | Map | ❌ | `` `set` is not lowerable on this tier yet — the Map value type has no representation here; use a hosted backend`` |

The harness's *reader* artifacts now cross to the substrate: the durable pipe
**reader** and the agent's toolbox (`Str.split`), and the web router's
`route_request` (`Str.indexOf`), lower to `$str_split`/`$str_index_of` over the
linear-memory `Str`/`List` ABI, closing the gap the concat-built **writer**
(`log_line`) already crossed. The three reader helpers are pulled into a module
only on demand (like `$f64_to_str`), so a component that never splits/joins/
searches keeps a byte-identical helper preamble. What still does **not** run on
the substrate is `repeat`, the `Map` operations, and `List.indexOf` (the
per-element comparison the harness's wire protocol never reaches); those are
emitted as compile-time refusals, so a wasm target never silently degrades.

## The service boundary

Two different boundaries exist, and they have different widths.

### Plain component services (`provide` / `requires` coeffects)

A service operation's declared param/return type crosses at the width of its
type: a scalar is an `i64`/`i32` **value**, a compound type an **`i32` pointer
into the calling module's memory** (the module exports `memory`, so a host —
or the cordis-wasm runtime — can read the result). This is the *same* canonical
ABI the value model uses, and it applies to v1/v2 components as much as v3:
the boundary is `_boundary_wty`, and its gate is `_check_type`. In practice:

- **Int, Bool, Str, Bytes, List, record, variant, Opt, Result** params and
  returns: supported (compound ones by pointer).
- **Float, Map, function types**: refused with the named `_check_type`
  diagnostic above.
- **Unit** (void op): no slot.
- A value whose wasm width disagrees with the declared type (a `List` returned
  from an `Int`-declared op) is refused rather than silently re-typed:
  `` a List value cannot cross this tier's scalar service boundary — the
  operation is declared Int; compound values stay inside the module ``.

> **History / why the FR-11 premise is stale.** The original substrate README
> said "non-Int component services" were refused ("the component tier carries
> scalars only"). The v3 value-model wave (docs/strings.md) widened the
> boundary: the linear-memory representation means a Str/List/record-returning
> service now lowers on the component tier too. The remaining hard refusals on
> this boundary are exactly `Float`/`Map`/function types.

### Instance accessors (`spawn` handles) and the config channel

Cross-*instance* provision reads are narrower: a rich (pointer) value would
cross as a pointer into memory the *spawner does not own*, so only scalars
cross the instance-accessor boundary:

```
s.k.op param 0 is Str — only scalar (Int/Bool) values cross the
instance-accessor boundary on this tier; a Str/record/List would cross as a
pointer into the instance's own memory (use a hosted backend)
```

and the same for a rich *return*. The config channel is scalar-only for the
same reason, and `config` blocks are refused outright on the component tier
(the runtime has no instantiation-config channel yet):

```
config blocks are not lowerable — the cordis-wasm runtime has no
instantiation-config channel yet (a spawn *target* is the exception...)
```

## Other hard tier restrictions

| construct | refusal |
|---|---|
| `config` blocks (component tier) | no instantiation-config channel yet — hosted backends |
| host builtins (`Map.new()`, `Pool.open(...)`) | `` host builtin 'Map' is not available on the cordis-wasm tier — express state through coeffects instead `` |
| method-time effects (`effect … undo …` in a method body) | the accumulator is fixed at activation (state machine) |
| method-time compensation (`undo` after an `emit`) | same |
| `await` outside `Job.run(name)` | `` `await` on this tier supports only `Job.run(name)` `` — the runtime's async host op (A1) |
| `match`/variants | ✅ now supported (tagged-union cells); the old "no tagged unions in core Wasm" README row is stale |
| non-scalar config *fields* | scalar-only, same reason as the boundary |

## Witnessed-effects teardown (item 243 Slice 2b)

The compiled teardown accumulator (`activate_step`/`deactivate`, above) now
carries THREE entry kinds, per docs/design/teardown-contract.md: `bracket`
(an ordinary `acquire`, unchanged), `transactional` (a `witnessed` extern's
declared inverse), and `compensation` (an `emit ... compensate ...`). Two
qualifications are specific to this tier and do not lift with this slice:

- **Over ACTIVATION-REGISTERED entries only.** The accumulator is fixed at
  activation (the state-machine row above: method-time effects and
  method-time compensation are both still hard `EmitError`s). The contract's
  "mixed-entry LIFO in both phases" therefore reads, on wasm, over the
  entries a component's top-level body registers — never a provide-method's.
- **Epoch/fuel is a wasmtime CAPABILITY the first-party driver wires, not a
  property of the compiled module.** The compiled WAT needs no special code
  for this — a Phase-2 `compensate` expression is an ordinary `call`, and
  wasmtime's epoch/fuel interruption is transparent to guest code once a HOST
  arms it. `backends/wasm/lifecycle.py`'s `drive_teardown` is that host: it
  drives the module's `deactivate_step` export one entry at a time (the same
  per-call idiom `activate_step` already uses), arming a fresh epoch deadline
  around each Phase-2 call. Host IMPORTS a compensation calls into are NOT
  preemptible this way — wasmtime only checks the deadline at a function-call
  entry or loop back-edge in GUEST code, so a compensation that blocks
  entirely inside a `req`/coeffect call is not cut off. This is the
  substrate's honest ceiling, not a partial implementation of the bound.

**Additive: the scaffold only appears on a component that actually registers
a `transactional`/`compensation` entry.** A component with no `witnessed`
extern and no `emit ... compensate ...` step has nothing for a commit/abort
split to distinguish (every entry is a `bracket`, which replays on commit AND
abort alike), so it emits `deactivate` in the exact single-pass shape it did
before this slice — no `$__committed`/`$__dstep` globals, no `committed()`/
`deactivate_step()` exports, byte-identical output. This mirrors the go/rust
Slice-2b landings' own gates (go's `_COMP_NEEDS_TEARDOWN`, rust's
`_body_has_witnessed`/`_body_has_compensate`). New exports, present only when
the gate is on (in addition to `activate_step`/`deactivate`, whose SIGNATURES
are unchanged either way, for backward compatibility with an existing host —
e.g. cordis-wasm's `Runtime.unplug` — that only knows the single-call
teardown hook):

| export | purpose |
|---|---|
| `committed() -> i32` | the abort-vs-commit discriminator: 1 iff `activate_step` ran every segment to completion without trapping |
| `deactivate_step() -> i32` | processes exactly ONE accumulator entry per call (1 = more remain, 0 = done) — a first-party host driver can catch a trap per entry (continue-and-record) and bound a compensation's epoch/fuel individually; `deactivate` itself is now a thin loop calling this to completion in one host call |

A `revl:teardown` custom section is emitted alongside `revl:isolate`/
`revl:intercept` (§v2 realms) whenever a component registers at least one
`transactional`/`compensation` entry: a STATIC, compile-time index (seq,
kind, `deactivate_step` dispatch position) of those entries, and the
Phase-1/Phase-2 dispatch-chain split. It is the honest ceiling of a "WAL
descriptor" on this tier: runtime argument/witness VALUES live in the
component's own linear memory, and this tier's teardown loop is compiled
state with zero host bookkeeping (the README's own framing), so there is no
channel here that durably persists a runtime value without a host choosing
to add one — this section is the STARTING INDEX such a host would build
against, not a claim of durability the substrate does not carry.
`backends/wasm/lifecycle.py`'s `parse_teardown_descriptor` reads it back.

## Lifecycle tests on the substrate (item 142)

A `lifecycle test` (docs/syntax-2.0.md §7.1) *runs* on the wasm tier when every
step stays inside the scalar instruction set. `revl test --backend wasm` boots
the emitted components on the live cordis-wasm runtime, calls through provision
keys (resolve the key in the coeffect table Σ, invoke its op), unloads LIFO, and
proves no residue — `len(rt.fibers) == 0` (R4) and the coeffect table `rt.table`
empty (R1), the substrate mirror of the once-mode boot's no-residue proof. The
driver lives in `src/revl/test.py` (`run_wasm`) + `backends/wasm/lifecycle.py`
(which steps the substrate can express) and `backends/wasm/lifecycle_harness.py`
(the cordis-wasm-side executor). `examples/lifecycle_wasm.rvl` is a scalar mesh
that runs.

What still **skips, per test, with a reason** (never a false pass):

| the test … | why it skips |
|---|---|
| `load C with { … }` | a `config` block does not lower — no instantiation-config channel |
| `call`/`assert` crossing a non-scalar boundary (`Str`/`List`/record/variant/`Opt`/`Result`/`Float`) | those cross as linear-memory pointers the host cannot marshal at the coeffect seam; only Int (i64) / Bool (i32) move across it |
| an `advance` step | timers (`every`/`after`, item 57) are not yet lowerable (docs/time-coeffect.md) |
| any component that does not lower (`Map`/`Pool`, method-time effects, …) | the whole mesh cannot boot, so its lifecycle tests skip with the emit reason |

`examples/lifecycle_cache.rvl` hits the first and last rows (`config`, `Pool`,
`Map`), so it skips on wasm while it runs on py/rust/ts/go. A skip is reported
as `[wasm] skip: … — <reason>`; a lifecycle test the substrate *can* express and
that then fails its assertion or leaves residue is a real `[wasm] fail:`, not a
skip.

## Writing to the wasm subset

A harness-shaped component that must also run on the substrate:

1. **Most string work lowers** — `length`/`push`/`concat`/`slice`/`charAt`/
   `charCodeAt`, plus the reader trio `split`/`join`/`Str.indexOf`. Only
   `repeat` and `List.indexOf` still need expressing with the others (or moving
   into a `fn` that stays on hosted tiers).
2. **Return Str/List/record/Opt/Result from services is fine** (linear-memory
   boundary); avoid `Float` and `Map` values anywhere, and avoid function-typed
   service params/returns.
3. **No config** on components that must boot on wasm (pass values through the
   composition instead), no host builtins, no method-time effects.
4. **Do not use `spawn` instance accessors with rich payloads** — scalars only.
5. Use `match` freely (tagged unions lower) and `await Job.run(name)`.

Every refusal above is a compile-time `EmitError` naming the tier — a wasm
target never silently degrades. The emitter's refusal sites are the single
source of truth: `_check_type`, `_builtin_type`/`_builtin_expr`,
`_boundary_wty`, the instance-accessor checks, and the config/host/await
guards in `backends/wasm/emit.py`. See also `backends/wasm/README.md` (runtime
shape and the `run --backend wasm` harness) and docs/strings.md (the
code-point unit and the Float-rendering fence).
