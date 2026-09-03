# Expressible bounded iteration in components — a design

**Status: implemented.** Arrow literals in provide-method bodies bind their
parameters in the method's scope, so the recursion-with-callbacks shape below
compiles today. The "Boundaries kept" section is still accurate for method
bodies; `while` and `for` did later arrive in the **`fn` statement grammar**
(item 379, [design/379-break-continue.md](design/379-break-continue.md)), which
is a different scope from the one this note rules them out of. This document
specifies how the agent
loop — the roadmap's lighthouse workload (the first product written in
revl) — becomes expressible *inside* a revl component. It is filed against
roadmap item 77(a) and the lighthouse workload's FR-1. Nothing here changes existing programs; every
change is additive.

## The problem, precisely

The harness's product is the agent loop:

```
prompt → model → tool calls → model → … → final, bounded by max_steps
```

When this note was written that loop **could not be written in revl**: the
harness shipped `step(session_id)`, one model call plus (maybe) one tool call
per invocation, with the iteration living in the host driver. The loop *is* the
product, and it was exiled to host code.

Three distinct walls stood inside provide-method bodies. **Wall 2 has since
fallen; walls 1 and 3 stand.**

1. **No `while`/`for`/`if` statements** in method bodies (G6: "revl bodies
   contain only effect forms"). The method-body statement set is exactly
   `let` / `var` / assign / `effect…undo…` / `emit` / `return`. Still true:
   `while` in a method body is a parse error citing G6. (`while` and `for` DO
   exist in the **`fn` statement grammar**, item 379. That is a different
   scope.)
2. ~~**Arrow parameters are not bound in provide-method scope.**~~ **Closed.**
   Arrow literals in provide-method bodies bind their parameters in the
   method's scope, so this compiles today:
   ```revl
   fn apply(p: Str, ms: List[Str], f: (List[Str]) -> Str) -> Str { return f(ms) }
   component App requires model: Model provides loop: Loop {
     provide loop {
       fn run(prompt) {
         let msgs = ["hi"]
         return apply(prompt, msgs, msgs2 => emit model.complete(msgs2))
       }
     }
   }
   ```
   The misdirecting `` `msgs2` is not a declared requirement of App `` hint is
   gone. What refuses now is honest emission coloring: because the arrow
   reaches `model.complete`, `Loop.run` must be declared `emission fn`, and G4
   says so with the reach chain. Declare it and the program is admitted. The
   conformance row is `method/arrow param binds in method scope (FR-1)`: `ok`
   on python, typescript, rust and go, `lim` on java and wasm (arrow values are
   not lowerable on either), and `lim` on the self-host column.
3. **A component cannot call its own provision** (recursion must go through
   a requirement), and **service methods are not first-class values**, so
   the "pass the operation" pattern (`model.complete` as a value) is closed
   too. Zero-param thunks *do* compile and capture method vars by value —
   but capture-by-value means they cannot see a `var` rebound across
   iterations, so they cannot carry a loop.

## Design

### Shape: arrow parameters bind in provide-method scope (77a smallest fix)

The smallest change that unblocks the recursion-with-callbacks pattern is to
make arrow literals in provide-method bodies bind their parameters in the
method's scope. The parser already records the arrow's parameter names
(`ExprArrow.params`); the missing piece is scope construction in
`src/revl/typecheck.py` and `src/revl/lower.py` when an `ExprArrow` is
encountered inside a method body: its `params` must be added to the
checking/lowering environment for the arrow's body, exactly as a top-level
`fn`'s parameters are.

Why this shape first:

- It is the smallest implementation (one scope-construction change per
  phase, no grammar, no IR, no emitter changes).
- It unblocks the harness's natural architecture — bounded recursion with
  callbacks:
  ```revl
  fn run_loop(msgs: List[Msg], step: (List[Msg]) -> Step, n: Int) -> Step {
    if (n <= 0) { return Final("max_steps exhausted") }
    return match step(msgs) {
      Final(answer) => Final(answer),
      NeedTool(req) => run_loop(msgs + [req.result], step, n - 1),
    }
  }
  component Agent requires model: Model, tools: Tools provides agent: Loop {
    provide agent {
      fn run(session_id) {
        let msgs = sessions.load(session_id)
        return run_loop(msgs, msgs2 => emit model.complete(msgs2), config.max_steps)
      }
    }
  }
  ```
  Top-level fns already recurse (probe: accepted), so the loop lives in a
  pure recursive fn; the component's contribution is binding the arrow's
  params so the *emitting* callback can be built in method scope.
- The emission-analysis gate already handles the crossing: a plain fn whose
  call chain reaches an emission is refused with "declared plain, but this
  implementation reaches … — mark it emission fn", so the recursive helper
  that calls an emitting callback is checked, not assumed (the harness
  already hit this and it worked: the refusal caught a design lie and the
  fix was honest).

### Boundaries kept (deliberately out of scope)

- **No `while`/`for`/`if` statements in method bodies.** G6 stays. The
  recursion-with-callbacks pattern is the checked, total way to iterate;
  the "bounded `while` in a method body" and "`loop`/`iterate` component
  form" shapes (FR-1 suggestions 2 and 3) remain open design candidates for
  a later item — they are strictly larger (grammar + IR + every emitter)
  and the callback-recursion shape covers the lighthouse workload.
- **A component calling its own provision** stays closed. Recursion goes
  through a pure fn; the emitting callback is passed in. If a later
  workload genuinely needs method-to-method self-calls, that is a separate
  item (the G1 bookkeeping rule — "`loop` is not a declared requirement" —
  is correct as-is).
- **Service methods as first-class values** stay closed. The arrow-literal
  form is the supported way to pass an operation.
- **No per-tier work**: arrow scope is frontend-only (parse → typecheck →
  lower). Emitters see the already-lowered call graph and are untouched.

## Semantics

- An arrow literal in *any* expression position inside a provide-method
  body binds its parameter names in the arrow's body scope, shadowing
  method params and method `let`/`var` bindings of the same name (normal
  lexical shadowing, same as a top-level fn).
- The arrow's body is checked exactly as today (a pure expression with
  `emit` allowed at a call site in a method body); only the *names in
  scope* change.
- Capture of method variables remains by value (unchanged); the pattern
  that needs to see a rebound `var` across iterations is out of scope (see
  Boundaries).

## Exit criteria

1. The FR-1 probe program (pure `apply` + `msgs2 => emit model.complete(msgs2)`
   in a provide method) compiles, and the returned value is the model
   response.
2. The harness's `run_loop`-shaped recursive fn (bounded by `n`, match on
   the `Final | NeedTool` union, callback carrying `emit`) compiles inside
   a component and runs on the py tier, returning the final answer after
   N iterations with max_steps respected.
3. A plain fn whose callback reaches an emission is still refused with the
   emission-analysis diagnostic (the gate is not weakened).
4. An arrow with an unbound *other* name still fails with the *correct*
   diagnostic — no more "add `requires msgs2: <Service>`?" for a name that
   is an arrow parameter; that class of misdirection is closed (FR-12).
5. No emitter output changes for any existing program (goldens byte-identical).

## Files

- `src/revl/typecheck.py` — bind `ExprArrow.params` into the arrow-body env
  in method scope.
- `src/revl/lower.py` — same for lowering.
- `tests/` — the exit criteria above as tests, including a
  rejection-file per the t8–t20 pattern for the misdirected-requirement
  diagnostic (FR-12) if it is not already pinned.
- `docs/guide-ai-agents.md` — one line: arrow params bind in method scope.
