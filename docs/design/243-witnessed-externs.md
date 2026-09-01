# Design: witnessed-inverse externs (item 243)

Status: design locked (Fable-reviewed 2026-08-26). Implementation in slices.

## The one thing to get right

A witnessed effect is **not** an `acquire`. They share the teardown accumulator
data structure and nothing else about their semantics:

| | `acquire` (bracket) | `witnessed` (transaction) |
|---|---|---|
| inverse replays on clean unload | **yes** (release the handle) | **no** |
| inverse replays on abort | yes | yes |
| on commit | n/a | **discharge the inverse + GC the witness** |
| success-path residue | none (that is the point) | the mutation IS the deliverable |

Lowering a witnessed effect like an acquire would restore every file the agent
deleted on a clean successful unload. So the accumulator gains a **second entry
kind** (`transactional`) with abort-only replay and a commit-time discharge+GC,
and item 243 lands that entry kind from day one even though item 245 lands the
commit UX.

## Surface (D1, D2, D3)

```
extern witnessed[fs] fn rm(path: Str) -> Result[FsWitness, FsError]
    undo restore(w: FsWitness)
```

- **`witnessed`** is a fourth extern classification alongside `pure`/`acquire`/`emission`
  (parser.py:309). Not `effect` (collides with activation `effect { }` and falsely
  implies bracket semantics); not `revertible` (an aspiration TOCTOU cannot keep).
- **Capability-scoped like an emission** (`witnessed[fs]`), and witnessed
  capabilities join the **same** authority namespace + policy/audit accounting as
  emissions, carrying a reversibility flag. They must NOT form a separate lattice,
  or fs authority becomes invisible to the item-33 policy gate.
- **Witness-as-return, auto-registered.** The effect returns the witness; the
  accumulator owns it; user code never spells a site undo and never threads the
  witness. The declared `undo` is the inverse that actually runs (see below).
- **Fallible.** Every fs op can fail (ENOENT/EPERM/disk-full). The effect returns
  `Result[Witness, Error]`; the inverse is registered **only on `Ok`** (an `rm` on a
  missing path returns `Err` and must not abort the session). `undo` binds the `Ok`
  witness.

## Correctness rules 243 must enforce (all new; "reuse acquire" does not give these)

1. **Declaration-owned, auto-registered inverse.** Today no backend consumes the
   extern-level `undo`; emitters read only the site-spelled step undo, which
   degenerates (`close_ledger(1)`). For witnessed effects the emitted code must
   register the **declared** undo automatically. Witnessed externs are therefore
   **refused outside effect position** (no bare call from a plain `fn`/`test` body).
2. **Fix the emission-analysis exemption.** `emission_analysis.py:70-71` deems an
   extern non-emitting because it *declares* an inverse. A bare witnessed call that
   registers nothing must NOT be classified revertible. Tie "revertible" to actual
   registration, not to the declaration.
3. **Inverse classification check (new).** The declared inverse must be classified
   **non-emission AND non-witnessed** (a witnessed inverse is infinite regress). It
   is a host-local restore. `undo some_emission(result)` passes lowering today via
   the shared `mode="undo"`; add the explicit check (this also closes a latent
   acquire hole).
4. **Witness is WAL-serializable data, not a host handle.** recovery.py:28-38: a
   closure inverse after a crash is residue, not recovery. `FsWitness` must be
   durable data (paths, refs) and the inverse WAL-reconstructible as a named call
   with captured arguments.
5. **Idempotent replay.** Abort replay can crash mid-way and `revl recover` replays
   again; inverses must be idempotent (or replay checkpointed). Rename-back run
   twice must not clobber.
6. **Inverse fallibility feeds 246.** `restore` can itself fail; an abort with
   restore-residue must surface a prompt, or "auto-approved because revertible"
   silently degrades to best-effort (that is 247's `compensate`, not 243).

## Slice plan

- **Slice 1 (this one): frontend + IR correctness core.** `src/revl/parser.py`,
  `typecheck.py`, `lower.py`, `emission_analysis.py`, `diagnostics.py`. The
  `witnessed[caps] fn ... -> Result[W,E] undo inv(w)` grammar; the fourth
  classification; effect-position-only; rules 1-3 above; the `transactional`
  accumulator entry kind in the IR with Ok-conditional auto-registration. Additive:
  no existing program uses `witnessed`, so the backends are untouched and stay green.
  Tested at the parse/check/IR level.
### Slice 1 as implemented (refinements)

Three decisions were refined against the actual code while landing Slice 1;
recorded here so Slice 2 builds on the real contract.

1. **`undo` reuses the acquire slot; `result` binds the `Ok` witness.** The
   surface above writes `undo restore(w: FsWitness)`, but the extern `undo` slot
   is an ordinary pure-expression call (`_check_extern_undo`), and acquire
   already binds the return as the implicit `result`. So the spelling is
   `undo restore(result)`, and for a `witnessed` extern `result` is typed as the
   **`Ok` payload** `W` (not the whole `Result[W,E]`), exactly the value the
   auto-registered inverse receives on abort. No new typed-binder grammar.
2. **`witnessed` is a CONTEXTUAL keyword.** It is recognised only in the extern
   classification slot, not added to the lexer `KEYWORDS`. This keeps the
   self-hosted lexer's keyword set in parity (its differential oracle asserts
   set equality) with no `selfhost/*` change, and never breaks a program that
   used `witnessed` as an ordinary identifier.
3. **The `transactional` entry kind is an IR descriptor on the extern node.**
   Slice 1 is "frontend + IR correctness core"; the per-call-site accumulator
   wiring is the Slice-2 runtime seam. So the second entry kind lands as a
   descriptor the runtime loop reads: a `witnessed` extern's IR node carries
   `class: "witnessed"`, `entry_kind: "transactional"`, `revertible: true`,
   `ok_conditional: true`, `witness: <W>`, `capabilities: [...]`, and the lowered
   `undo`. An `acquire` node carries `undo` but **no** `entry_kind` (its effect
   step is the existing bracket), which is the checked distinction. Rule 1's
   "refused outside effect position" is enforced as a refusal of any witnessed
   call in a `fn`/`test` body (no accumulator there); the positive effect-position
   call site is enabled with the runtime seam in Slice 2.

- **Slice 2: six-tier runtime seam.** Each backend's emit + runtime teardown loop
  consumes the declared inverse, auto-registers it, and implements the
  `transactional` entry kind (abort-only replay + commit discharge + witness GC),
  plus WAL descriptor emission. Async-extern-scale (items 80/115). Rust part waits
  for item 278.
### Slice 2a as implemented (py reference-tier runtime seam)

The py teardown seam is landed in `backends/python/{emit,runtime}.py`; the other
tiers (Slice 2b) follow this contract.

1. **The abort-vs-commit discriminator is "did `drain` run".** The emitted body
   is one cordis effect whose yielded disposers cordis unwinds LIFO. A clean
   activation runs to its final `yield _revl_frame.drain`, so on unload `drain`
   is disposed FIRST and every earlier disposer runs after it; a mid-activation
   failure raises before that `yield`, so cordis's setup-failure unwind replays
   the already-collected disposers and `drain` never runs. `Frame.drain` flips
   `Frame._committed = True` synchronously at entry, so a transactional disposer
   reads `_committed == True` on a clean commit and `False` on an abort. This
   needs no new cordis signal and no lowerer change.

2. **The transactional entry is a distinct disposer, not a distinct list.**
   `Frame.transactional(undo, witness)` returns a `_Transactional` disposer that
   joins the same LIFO disposer stack as every bracket inverse (so mixed-entry
   LIFO is preserved for free — for effects fired from the ACTIVATION body;
   point 5 states the rule for effects fired mid-session, from a provide method)
   and, at disposal time, replays `undo(witness)`
   iff `not frame._committed` (abort) and otherwise discharges — dropping both
   the inverse and the witness references (witness GC). A bracket (`acquire`)
   still `yield lambda: <undo>`s and replays unconditionally: the two entry
   kinds are now observably distinct at runtime (clean unload reverts the
   bracket, persists the witnessed mutation).

3. **Registration is Ok-conditional and uses the DECLARED inverse.** The emitted
   call site runs the mutation, and on the `Ok` branch (`isinstance(x, Ok)`)
   yields `_revl_frame.transactional((lambda result: <declared undo>), x.value)`
   — the extern's own `undo` with the `Ok` payload bound as `result`; on `Err`
   it registers nothing. There is no site-spelled undo; the accumulator owns it.
   emit keys this off the acquisition calling a `witnessed` extern (the externs
   table), so no new IR step field is required and every non-witnessed program
   emits byte-identically.

4. **Deferred: the effect-position call-site SURFACE.** Slice 2a is the backend
   consuming the IR; it did NOT touch `src/revl/lower.py`. A witnessed call in
   effect position without a site undo (`effect rm(p)`) is still refused by the
   lowerer — enabling that surface (auto-attaching the declared undo, stamping
   the step) is a lower.py slice that belongs with 245's commit UX, kept out of
   Slice 2a to avoid colliding with item 312's live lower/main work. The runtime
   seam is proven against the IR the future lowerer will emit (a standard
   `effect`/`let-effect` step whose acquisition calls a witnessed extern), which
   is exactly the shape emit already handles.

5. **Mid-session (post-activation) witnessed effects replay LIFO across the
   whole frame — item 318 seam, item 369 fix.** Points 1–2 reason about the
   ACTIVATION body, whose disposers the host runtime unwinds LIFO. But the real
   agent case is a witnessed effect fired from a PROVIDE METHOD, per tool call,
   AFTER the component activated. Such an effect has no body generator to
   `yield` its disposer into, and adopting it as a sibling effect is unsound
   (the host disposes an adopted effect BEFORE the body's `drain`, so a clean
   unload would see `_committed` still `False` and wrongly revert the
   deliverable). So `Frame.transactional_method` PARKS the entry in
   `_deferred_transactional` and `Frame.drain` disposes it once the
   commit-vs-abort bit is settled — commit discharges it, abort replays its
   inverse.

   The contract: **a mid-session witnessed inverse replays in reverse
   INVOCATION order (LIFO) across the whole frame — identical to an
   activation-body inverse, and consistent with G7.** `_deferred_transactional`
   is appended newest-last as each provide method fires, so `drain` must dispose
   it newest-FIRST (`reversed`). On a commit the order is immaterial (every
   entry no-op discharges); on an ABORT it is load-bearing: two inverses whose
   paths OVERLAP must undo newest-first, or — because every stdlib/fs.rvl
   inverse is idempotent-and-total ("a second replay is a no-op") — the oldest
   inverse runs first, finds nothing, silently no-ops, and the newer inverse
   then undoes into the hole. A FIFO drain therefore leaves residue or DESTROYS
   pre-session data while abort still reports `noResidue: true`, silently
   voiding the item 246 auto-approve of a class-(a) crossing. Three-line
   reproducers (`mv a b ; mv b c`; `rm a ; touch a`; `write "V2" ; write "V3"`,
   each then `abort`) are in `tests/test_witnessed_abort_lifo.py`.

   This holds on every tier that carries the deferred-park mechanism (py
   `runtime.py`, ts `runtime.ts`, go `emit.py`'s `RevlFrame.commit`): each
   drains the parked list newest-first. Tiers that flip `committed` eagerly at
   activation-end (rust, java) register the method inverse directly on the host
   runtime's native LIFO dispose stack and need no parked list; the wasm tier
   parks in a newest-first linked list and pops the head first. All four are
   LIFO by construction.
- **Slice 3: item 244 `stdlib/fs.rvl`.** The per-tier witness bodies (APFS
  clonefile / copy fallback / rename-to-garbage). First visible H1 proof.
