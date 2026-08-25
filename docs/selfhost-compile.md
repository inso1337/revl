# The capstone: the integrated native `revl_compile`

Roadmap item 224. The self-host effort ported the compiler one pass at a time —
`selfhost/lexer.rvl`, `parser.rvl`, `checker.rvl`, `lower.rvl` for the frontend,
`emit_py.rvl` / `emit_rust.rvl` for the tail — each a **differential oracle**
against the reference. This is the payoff: `selfhost/compile.rvl`, the driver
that composes those stages into one revl-native pipeline, and
`tests/test_selfhost_compile.py`, which proves the composed chain produces target
source **byte-for-byte identical** to the reference `backends/<tier>/emit.py` on
the surface all six stages cover — for **py and rust**.

This note states precisely what "revl compiles itself" means as of this slice:
what is native end to end, and the two seams that remain before a single-call
in-file native `source → target`.

## The pipeline

```
source ──▶ compile.rvl `compile_to`        the native FRONTEND, one artifact:
                                           lexer.rvl → parser.rvl → the checker
                                           → the lowering ADMISSION gate
                                           (lower.rvl `use`s lexer+parser and
                                           reimplements the rest). Verdict only:
                                           "" admits, "<TAG>|<msg>" refuses.
       ──▶ [interchange IR]                docs/backend-ir.md — the JSON every
                                           backend consumes.  SEAM 1.
       ──▶ emit_py.rvl / emit_rust.rvl     the native EMITTER, a separate
           `emit_src`                       artifact per tier: IR → target
                                           source, byte-exact vs the reference.
```

`compile_to(source, tier)` in `compile.rvl` **is** the front half, in one
co-compiled artifact: it runs the full native frontend admission over the source
and selects the tier.

- unsupported tier → `UNKNOWN_TIER|<tier>`
- the native gate refuses → `REFUSED|<TAG>|<message>` (the emitter is never
  reached — a program the checker rejects cannot be compiled, the one revl
  promise, carried through the composition)
- admitted → `ADMITTED|<tier>` — the go-ahead for that tier's native emitter.

## What is native, end to end, and proven

On the **function + simple-component surface** — the checked-in emitter corpora
(`tests/fixtures/emit_py_corpus/`, `emit_rust_corpus/`), which are exactly the
documents the self-host emitters are held byte-exact on:

- **The frontend is native and admits the whole emit surface.** For every corpus
  document, the native gate returns `ADMITTED|<tier>`, agreeing with the
  reference's admission (`test_native_gate_admits_the_emit_surface`). Its
  verdict-parity with the reference on *rejected* programs — the same guarantee
  tag and message — is proven exhaustively in `tests/test_selfhost_lower.py`;
  here the composed driver additionally refuses every reference-rejected program
  **before any emitter runs**, for both tiers
  (`test_refused_program_never_reaches_an_emitter`).
- **The emitter is native and byte-identical to the reference.** For every corpus
  document, the native `emit_src` output equals `backends/<tier>/emit.py`'s to
  the last byte, for **py (17 documents) and rust (15 documents)**
  (`test_native_pipeline_is_byte_identical`).

So the native span that composes end to end and yields byte-exact reference
target source is: **the native frontend admission gate, then the native
emitter** — with the interchange IR between them being the one non-native seam.

`pytest tests/test_selfhost_compile.py -q` → **41 passed**.

## The two seams (what remains for full native `revl_compile`)

Both are named in `selfhost/compile.rvl`'s header. Each is a concrete follow-up.

### SEAM 1 — `lower.rvl` yields a VERDICT, not the IR

This is the integration gap the capstone anticipated. `selfhost/lower.rvl` was
built as an **admission gate**: `admit_src` decides admit/refuse and names the
guarantee, but it does **not produce the interchange IR** that `emit_py.rvl` /
`emit_rust.rvl` consume (the standard `ir_version: 3` document the reference
`lower.py` produces — `docs/backend-ir.md`). There is therefore no native
`source → interchange-IR` producer, and the native frontend cannot hand its
lowered IR to the native emitter.

In the end-to-end proof the IR between the native front and the native tail is
supplied by the reference lowering (`revl.compile_source` / `compile_files`).
That is the exact, and only, *data* seam.

- **What `lower.rvl` outputs:** a verdict string — `""` (admit) or
  `"<TAG>|<message>"` where `TAG ∈ {G1, G2, G3, G4, A1, PRELUDE, SPAWN, HANDOFF,
  BAD}`.
- **What the emitters need:** the full interchange IR — `{ ir_version, functions,
  components, services, types, externs, … }`, navigated by the emitters through
  `stdlib/value.rvl`'s `value_*`.
- **Follow-up:** `lower.rvl` must also **emit the interchange IR** (the emittable
  document), not only the verdict. Once it does, `ir` disappears from the pipeline
  and `source → target` is native from the first byte of source to the last byte
  of target.

#### SEAM 1, first cut (roadmap item 227): `lower_to_ir`

`selfhost/lower.rvl` now carries `pub fn lower_to_ir(src) -> Str` beside
`admit_src` (additive — `admit_src` is byte-for-byte unchanged). It serialises
the **covered surface** of the interchange IR as canonical JSON, structurally
identical to `revl.compile_source` on the same source
(`tests/test_selfhost_lower_ir.py`, cross-checked over the whole emit_py corpus):

- **Covered, byte-identical:**
  - `ir_version` — from the header-visible feature triggers (a module
    fn/extern/type/test → 3; a component `if`/`fail`/timer/`spawn` → 3; an
    `isolate`/`intercept` → 2; else 1);
  - the **services table** — every service's methods with
    `params`/`returns`/`emission` (+ `capabilities` for a scoped `emission[..]`,
    + `async`);
  - each component's **declaration header** — `config`/`requires`/`provides`;
  - the component **body** for the simple-component surface (`effect …/undo …`
    and `provide` steps over required-service calls + literals) — emitted only
    when the whole body stays in surface (the `services_basic` intersection),
    else omitted so the header still lands and the richer body defers.
- **Emitter-ready proof:** the reference python emitter applied to the *native*
  IR produces the **same bytes** as applied to the reference IR for
  `services_basic` — the native IR is emitter-ready end to end for that surface.
- **Deferred (the residue of the data seam):** the `functions` / `externs` /
  `types` sections, the `manifest`/`holes`/`tests`/`fault_tests`/`prop_tests`
  sections (link/obligation artifacts the covered emitter surface does not read),
  the per-component `source` (the input filename, an environment artifact), and —
  the bulk — the **typed component/method expression body**: arithmetic
  (`typed_arith`/`widen`), stdlib builtins, `config` reads, `match`/ADT, saga
  `emit … compensate`, timers and spawn steps. This is lower.py's typed-
  expression spine (`_lower_pure_expr` / `_lower_component_pure_expr` /
  `_lower_fns` / `_lower_type_decls`), which needs the static-type machinery the
  admission gate does not carry. One `ir_version` trigger lives there too: a
  stdlib builtin in a body bumps the reference to 3 (`_has_builtin`), which the
  header producer under-approximates (the single corpus case, `services_methods`).

Closing the full data seam is the follow-on: port the typed-expression spine so
`lower_to_ir` produces the complete `functions`/component-body IR the emitters
consume for the whole corpus.

### SEAM 2 — the stage modules do not co-compile into one composition

A second, structural gap surfaced while wiring the driver. Each heavyweight stage
was built to compile **alone** against the reference, so their declarations
collide when merged into a single revl composition:

- `use "./lower.rvl"` beside `use "./emit_rust.rvl"` → **duplicate type `Ctx`**
  (both declare a private `Ctx` record);
- `use "./lower.rvl"` beside `use "./emit_py.rvl"` → a cross-module type error
  (`contains(...)` resolves to `List[Str]` where `Str` is expected);
- `use "./emit_py.rvl"` beside `use "./emit_rust.rvl"` → a cross-module
  `for ... of` iterates `Any` error.

The module loader merges every used module's declarations — **including private
ones** — into one program for lowering, so two stage modules that each name a
private `Ctx` (or that shift each other's overload resolution) cannot share a
composition. The frontend chain already co-compiles (`checker.rvl` and
`lower.rvl` both `use` `lexer.rvl` + `parser.rvl`); it is the **emitters and
`lower`** that will not co-compile with each other.

That is why `compile.rvl` co-compiles exactly the frontend span (`lower.rvl`) and
the emitters remain **separate native artifacts**, chained by the test harness
rather than by a single `use`.

- **Follow-up:** reconcile the stages' private declarations (rename the colliding
  private `Ctx`s and the overload-shifting helpers), or give the module system a
  private-namespace rule so a used module's non-`pub` declarations do not enter
  the importer's global table. Either turns `compile.rvl` from the front half into
  an in-file `source → target` that `use`s the gate **and** both emitters
  directly.

## Files

- `selfhost/compile.rvl` — the driver (`compile_to`, `admit`, the
  `CompileStream`/`Compile` plugin wrapper, in-file `test` blocks).
- `tests/test_selfhost_compile.py` — the byte-exact composition proof (py + rust)
  plus the refusal-composes and tier-guard tests.
- Reference-only, read: `selfhost/{lexer,parser,checker,lower,emit_py,emit_rust}
  .rvl`, `src/revl/compiler.py` (`compile_source`/`compile_files`),
  `backends/{python,rust}/emit.py`.
