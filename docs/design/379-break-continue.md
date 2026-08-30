# 379: `break` and `continue` in loops

Design note for roadmap item 379 (`docs/v2.0-roadmap.md:4090`). This is
design-first per the item's own flag: the semantics of teardown across an early
loop exit in a typed-effect language must be settled before any parser or
emitter code is written, because a wrong choice here reintroduces the exact bug
class item 247 exists to prevent (a disposer firing on a clean path, or
teardown skipped on an exit path). The note records the measured problem, how
loops and teardown are shaped today, the teardown-on-exit decision and why the
tempting alternative is unsound, the surface and typing deltas, per-tier
codegen for all six backends, a staged plan an implementation agent can pick
up, and exit tests including the soundness set.

## The problem (measured)

revl has `while` and `for (x of xs)` but no `break` or `continue`, so a loop
that decides mid-body to stop must set a `done`/`handled`/`isf` flag the
header re-checks. The roadmap names three costs (`docs/v2.0-roadmap.md:4090`),
in priority order:

- **Ergonomics and correctness.** Flag boilerplate is a bug class: forget to
  set the flag, or re-check the wrong one, and the loop spins or exits a step
  late. The self-host lexer (`selfhost/lexer.rvl`) carries 18 such loops.
- **Agent first-try correctness.** `break` is muscle memory in every language
  an authoring model knows. Today `break` lexes as an identifier
  (`src/revl/lexer.py:9-31` has neither word; `lexer.py:292` demotes a
  non-keyword to `ident`), so a bare `break` statement in a fn body parses as
  `ExprStmt(ident)` and reports the misleading G1 "`break` is not declared in
  this function", the exact actively-wrong-redirect failure class item 384
  catalogued. The 384 redirect table already anticipates this pairing (the
  C-style-`for` redirect at `src/revl/parser.py:2721-2741` cites "pairs with
  379").
- **Perf, modest and bench-gated.** A flag-guarded early-exit loop pays a
  per-iteration boolean check plus one redundant final iteration. The roadmap
  is explicit that this is a compare, not an allocation (contrast item 276),
  and that most of the lexer's 18 loops are scan-while-condition loops with
  the exit already in the header, which gain nothing. The perf claim is sold
  bench-gated on the self-host lexer or not at all.

## The current shape

### Lexing and parsing: two loop forms, one statement grammar that has them

Neither `break` nor `continue` is a keyword (`src/revl/lexer.py:9-31`). Both
words are on every backend's reserved-target-word mangling list already
(`backends/java/emit.py:113-114`, `backends/go/emit.py:3034`,
`backends/rust/emit.py:80`, `backends/typescript/emit.py:56`), so a revl
program may currently bind `let break = 1` and each emitter renames it. A
repo-wide sweep finds no `.rvl` source using either word as an identifier
(all hits are inside emitted-string literals and comments), so keywordizing
is safe for the in-repo corpus; the harness corpus needs the same one-line
sweep before Stage 1 lands.

The loop forms are exactly two, both in the **fn statement grammar**
(`fn_stmt`, dispatch at `src/revl/parser.py:2648-2651`):

- `while (cond) { ... }`, `while_stmt` at `parser.py:2711`;
- `for (x of xs) { ... }`, `for_stmt` at `parser.py:2719`, with the item-384
  redirects for C-style `for` and `for (x in xs)` at `parser.py:2721-2752`.

The fn statement grammar reaches module `fn` bodies, `test` bodies
(`parser.py:2264`, `2285`), and loop/if bodies recursively. The
component/method statement grammar (`Parser.stmt`, `parser.py:1567`) has **no
loop form at all**: activation bodies and provide-method bodies cannot
contain `while` or `for` today, and a provide-method match-block arm refuses
them explicitly (`_refuse_block_arm_stmt`, `src/revl/lower.py:4126-4144`,
`code=G6, category=block-arm`). Block-effect setup refuses control flow too
(`lower.py:4680`).

### Lowering and the flow analysis that names this feature

`_lower_pure_stmt` lowers the two forms to `{"step": "while", "cond", "body"}`
(`lower.py:2826-2834`) and `{"step": "for", "bind", "iterable", "body"}`
(`lower.py:2835-2858`). There is no `break`/`continue` step kind anywhere in
the IR, and no emitter has an early-exit path (confirmed by sweep: the only
grep hits across all six `backends/*/emit.py` are the reserved-word lists
above).

The return-path analysis is where the absence is load-bearing.
`_definitely_returns` (`lower.py:958-983`) implements the Java/Rust
conservative rule and says so in its docstring, including the line this item
must rewrite:

> `while (true)` diverges (there is no `break` in the grammar), so nothing
> after it is reachable — Java and Rust agree, and no tier needs a value.

`_has_return` (`lower.py:944-955`) and `_check_returns_on_every_path`
(`lower.py:985`, called at `lower.py:1129`) sit on top of it. The wasm
emitter has its own copy of the same judgment (`_diverges`,
`backends/wasm/emit.py:4763-4785`), because the wasm validator does no flow
analysis and the emitter must know when a fallthrough value is needed.

### Teardown: activation-scoped, and no registration form can reach a loop

The teardown contract (`docs/design/teardown-contract.md`) is one LIFO
disposer stack **per activation**: three entry kinds (`bracket`,
`transactional`, `compensation`), commit runs brackets and discharges the
rest, abort runs Phase 1 proof replay then Phase 2 best-effort compensations.
The py reference carries it as `Frame` (`backends/python/runtime.py:817`)
with `compensation` (`runtime.py:1191`), `compensation_method`
(`runtime.py:1236`), and the session escrow in `SessionOwner`
(`runtime.py:1614`) whose `finalize_abort` (`runtime.py:1915`) drains
Phase 2.

Every form that registers an entry on that stack lives in the
activation/method statement grammar, not the fn grammar:

- `let x = effect ... undo ...` (bracket) and `spawn` acquisitions:
  `parser.py:1598-1614`, emitted as `_revl_frame.acquire`/`adopt`
  (`backends/python/emit.py:1380-1393`);
- witnessed Ok-conditional registration: `_revl_frame.transactional` /
  `transactional_method` (`emit.py:1181`, `1209`);
- `emit ... compensate ...`: activation-body site `emit.py:1049-1061`
  (`yield _revl_frame.compensation(...)`), method-body site
  `emit.py:1400-1417` (`_revl_frame.compensation_method(...)`), lowered at
  `lower.py:6297`;
- approval acquisition and deferred enqueue: `emit.py:1105`, `1144`.

An extern-declared `compensate` slot lowers onto the extern IR entry
(`lower.py:2256`) but registration still happens only at `emit` steps; a
module fn body never references a frame in any backend (the fn-body statement
emitter at `emit.py:2048-2061` is frame-free). A module fn that transitively
reaches an emission extern is an emitting fn (the emission fixed point,
`lower.py:3696-3697`) and its calls are bare crossings on the audit surface:
fire-and-forget, nothing registered, nothing to discharge.

So the two grammars are disjoint on exactly the axis that matters: **loops
exist only where registration cannot happen, and registration exists only
where loops cannot happen.** No emitter wraps a loop in teardown scaffolding
on any tier. This is the fact the whole design leans on, and the fact the
design must also guard, because it is an accident of grammar today, not a
stated invariant.

## The teardown decision: `break`/`continue` are frame-neutral, and that is the theorem, not a shortcut

The design-first question: when a loop iteration is abandoned early, what
teardown runs? The answer, made normative here:

**Decision 1: no revl teardown boundary coincides with a loop boundary.**
Teardown boundaries are activation boundaries (and, under item 245, the
session commit point). `break`, `continue`, an early `return`, and normal
end-of-iteration all run exactly the same amount of frame teardown: none.
The accumulator is untouched by loop control flow in every direction:
nothing is registered, nothing is run, nothing is discharged, nothing is
reordered.

Concretely, per exit path:

- **`continue`**: transfers control to the next iteration's condition test
  (`while`) or to the increment-then-test (`for`). No end-of-iteration
  teardown exists today for a completed iteration, so an abandoned iteration
  owes exactly the same: none. Any entries a (future) registering form had
  pushed before the `continue` stay on the frame in registration order.
- **`break`**: transfers control past the loop. Same rule. Statements after
  the loop see the frame exactly as a header-flag exit would have left it.
- **early `return` through a loop**: already legal today (`_has_return`
  walks loop bodies, `lower.py:952-953`) and already frame-neutral; a fn
  return does not drain anything on any tier. Unchanged.
- **abort through a loop** (a `fail`/A8 L-Raise or host raise inside a fn
  called from an activation): unchanged. The unwind propagates out of the
  loop as out of any statement; the activation frame drains per the teardown
  contract, Phase 1 proof replay LIFO over everything registered up to the
  abort point, then Phase 2 compensations. Loop structure is invisible to
  the drain, exactly as it is invisible today.
- **emissions already fired in an abandoned iteration**: a `break` does not
  un-emit. A crossing that fired stays on the G8 audit surface with its
  three-state tag; if it registered a compensation (only possible from
  activation/method context today), that entry stays owed for abort-time
  Phase 2 and discharges on commit. This is the honest semantics: the
  emission left the system before the `break` ran.

### Why the tempting alternative is unsound

The alternative is iteration-scoped teardown: treat each iteration as a
sub-scope, run "that iteration's teardown" on `continue`/`break`. This is the
design that reintroduces the 247 bug class, on three independent grounds:

1. **It fires abort-only entries on a clean path.** A `compensation` entry is
   abort-only and discharged on commit (`teardown-contract.md`, the
   algorithm's commit branch; 247 Decision 1). A loop exit is not an abort:
   the activation is still on its way to a clean commit. Running an
   iteration's compensations at `break` time is precisely the 247 placeholder
   bug ("the DB `compensate db.delete(row.id)` fires on a clean, successful
   withdrawal too, deleting the row the insert was supposed to deliver",
   `docs/design/247-compensate.md`). Discharging them early is equally wrong:
   an abort later in the same activation would then skip offsets it owes.
2. **It splits the LIFO stack and the WAL seq order.** Popping an iteration's
   bracket entries early means the frame's drain order no longer equals
   reverse registration order, and the WAL descriptors' `seq` (replay order
   is reverse-seq within phase, `teardown-contract.md`) no longer describes
   what an abort would do. Every consumer of the contract, `revl recover`
   included, would need an iteration-scope concept that does not exist in
   the schema.
3. **The wasm tier cannot represent it.** The wasm accumulator is fixed at
   activation time as a compiled dispatch chain with a static `revl:teardown`
   section (`backends/wasm/emit.py:386-427`, `2064-2066`); method-time
   compensation is already a hard `EmitError` on that tier
   (`teardown-contract.md`, wasm qualifications). Per-iteration dynamic
   registration counts have no representation there.

Frame-neutral `break`/`continue` satisfy G5 (teardown registers nothing:
nothing runs at all), G7 (derived LIFO teardown: the derivation is untouched),
and A8 (mid-body failure reverts and contains: the abort path is unchanged)
by construction rather than by new machinery. The roadmap's fear, "a
`break`/`continue` out of a scope that acquired resources must still unwind
that scope's disposers correctly", is answered by the current shape: the only
scope that acquires is the activation, and its disposers unwind at the
activation boundary whether or not a loop inside a called fn exited early.

### Decision 2: make the grammar accident an enforced invariant

The frame-neutrality theorem rests on the grammar disjointness, which some
future item could silently break (loops in activation bodies, or a
registering form reachable from fn bodies; item 383's `.map`/`.filter` and
any future `emit` in emitting fns are the plausible pressure points). Two
guards, both cheap:

- A lower-time assertion in the loop cases of `_lower_pure_stmt`: no lowered
  step inside a `while`/`for` body may be a registering kind (`effect`,
  `emit` with a `compensate`, `transactional`, approval, deferred, spawn).
  Today this is unreachable by grammar; the assertion turns "unreachable"
  into "refused with a message naming this doc" the day it stops being so.
- A standing sentence in this doc, for the future item that wants loops and
  registration to meet: it must decide per-iteration accumulation semantics
  explicitly (unbounded frame growth across iterations, entry provenance,
  and the wasm static-chain gap) and amend `teardown-contract.md`. Loop
  control flow stays frame-neutral even then; what that item decides is
  registration-in-a-loop, not exit-time teardown.

The composition that IS reachable today, and that the exit tests pin: an
activation body acquires (bracket), registers a witnessed inverse or a
compensation, then calls an emitting fn whose loop `break`s or `continue`s
mid-iteration. Every entry must behave exactly as if the loop had exited via
a header flag: release/replay exactly once on the right path, discharge on
commit, never at `break` time.

## Surface and refusals

### Keywords and statements

Add `break` and `continue` to `KEYWORDS` (`src/revl/lexer.py:9-31`), to the
formatter's list (`src/revl/formatter.py:74`), and to the self-host lexer's
table when the 391 port lands. Two new AST statements, `BreakStmt(line)` and
`ContinueStmt(line)`, parsed in `fn_stmt` (`parser.py:2648`) only. Bare
keyword, no label, no value: labeled break is out of scope (revl has no other
label concept; the flag-loop fallback still exists for the rare two-level
exit, and a labeled form can be its own item if the need is ever measured).

### Where they are valid, and the refusals

Valid position: inside the body of a `while` or `for`, at any statement
depth, in any fn-grammar context (module fn, test body, nested `if`). The
parser tracks a loop depth counter (incremented around the body parses at
`parser.py:2715`/`2755`); this is parse-state, matching `_assign_ahead`-style
local lookahead, and costs nothing.

Refusals, all parse-time, in the item-384 redirect voice (`Parser.err`,
`parser.py:913`, message plus hint; parser refusals carry no G-code today and
these follow suit, classified by message shape like the 384 redirects):

- **Outside a loop, fn grammar** (depth 0): "`break` is only valid inside a
  `while` or `for` body", hint pointing at returning early or restructuring
  (and, for `continue`, at the header condition). This replaces today's
  misleading G1.
- **Component/method grammar** (`Parser.stmt`, `parser.py:1567`): the words
  are now keywords, so they would otherwise die as a generic "expected a
  statement". Add an explicit redirect: "`break` is not valid here:
  activation and provide-method bodies have no loops", hint pointing at
  lifting iteration into a module `fn` (the same redirect shape
  `_refuse_block_arm_stmt` uses for loops themselves, `lower.py:4126-4144`).
- **As a binder**: `let break = 1` now fails as "expected ident, found
  keyword". Acceptable; the corpus sweep above says nobody does this, and
  the 384-style redirect is not owed for a name nobody uses.

Semantics note for `docs/syntax-2.0.md` §3.5 (`syntax-2.0.md:171`): both
statements are TS-verbatim, which is the section's standing rule for loops.
In particular `continue` in a `while` re-tests the condition WITHOUT running
anything after the `continue`, including a trailing `i += 1`; that is every
C-family language's behavior and precisely what an authoring agent expects,
so matching it verbatim is the first-try-correctness play. No new semantics
invented.

### Unreachable code after `break`/`continue`

Method bodies refuse an unreachable statement after `return`
(`lower.py:5886`); fn bodies do not, and `return` mid-fn-body is silently
accepted with dead statements after it today. Decision: v1 matches the
fn-body `return` precedent, silent. A statement after `break` is dead but not
refused, exactly as after `return` in the same position. A dead-code
diagnostic for fn bodies is one uniform future change covering `return`,
`break`, `continue`, and `fail` together, not a special case here.

## Typing and flow analysis

`break` and `continue` are statements with no expression payload, so there is
nothing to type and nothing for the item-386 `POISON` sentinel
(`src/revl/typecheck.py:320-336`) to absorb or propagate; multi-error
recovery is untouched at the expression level. Two honest notes on 386
composition: fn lowering runs in the phase-2 table-building stretch of
`check_and_lower`, which keeps raising (386 Stage 1's truncation behavior),
so a `break`-outside-loop refusal truncates the report exactly as every fn
refusal does today; and the parse-time refusals are single-shot until 386
Stage 3 (parser recovery) exists. Neither is a regression; both are the
standing 386 boundaries.

The real work is `_definitely_returns` (`lower.py:958-983`). Its
`while (true)` clause is justified by "there is no `break` in the grammar";
this item deletes that justification, so the clause must become break-aware,
and the fix is the same Java rule the docstring already borrows (JLS 14.21):

- `while (true)` (a literal `true` condition) terminates the path iff its
  body contains **no reachable `break` that targets it**. With bare
  (unlabeled) `break` only, "targets it" is: a `break` anywhere in the body
  that is not inside a nested `while`/`for`.
- A `break`/`continue` statement itself completes abruptly: a path through
  it does not fall off the end of the fn, but neither does it return. For
  the conservative analysis this means `_definitely_returns` needs no new
  positive case (a `break` never proves a return); only the `while (true)`
  clause changes.
- `_has_return` (`lower.py:944`) is unchanged: it already walks loop bodies
  and asks only whether a `return` exists somewhere.

Portability is the criterion the docstring itself sets ("a body this accepts
is a body those tiers accept"): Java refuses a missing return after
`while (true) { ... break; }` and accepts it after `while (true) { }`; Rust
types `loop { }` as `!` and a `loop` with a `break` as unit. The break-aware
rule keeps revl aligned with both, so nothing the frontend accepts becomes a
tier error. The wasm emitter's `_diverges`
(`backends/wasm/emit.py:4763-4785`) is the same judgment on the emit side
and gets the same rule in Stage 4, or wasm emits a function whose fallthrough
arm is missing a value.

The `verified fn` totality gate (`_check_verified_totality`,
`lower.py:920-942`) is recursion-based and unchanged; its hint already
gestures at "a syntactically bounded loop" and `break` neither helps nor
harms that conservative check.

## IR

Two new step kinds, additive: `{"step": "break"}` and `{"step": "continue"}`
(a `line` for diagnostics as siblings carry). No payload, no flags. A program
without them produces byte-identical IR and byte-identical output on all six
tiers, the standing additivity property (342/388 precedent). The IR change is
one line each in `_lower_pure_stmt` plus the outside-loop guard; the loop
cases at `lower.py:2826`/`2835` do not change shape.

## Per-tier codegen

Five tiers are one-line native mappings; wasm is the real work.

| tier | while / for emit site | break/continue lowering |
|---|---|---|
| python | `backends/python/emit.py:2048` / `:2055` | native `break` / `continue` |
| typescript | `backends/typescript/emit.py:2330` / `:2335` | native |
| go | `backends/go/emit.py:4165` / `:4170` | native |
| rust | `backends/rust/emit.py:4393` / `:4398` | native |
| java | `backends/java/emit.py:1686` / `:1691` AND `:3352` / `:3357` | native, in BOTH lowerers |
| wasm | `backends/wasm/emit.py:4746-4758` / `:4787-4831` | labeled `br`, below |

Per-tier notes, each verified against the emitter:

- **python/typescript.** `while`/`for-of` map to the same construct;
  `break`/`continue` are the host statements. No frame interaction exists in
  fn bodies (the emitters' try/finally scaffolding is activation/session
  scoped, never loop scoped), so teardown-neutrality is literal.
- **go.** `while` is Go's condition-only `for` (`go/emit.py:4165-4169`) and
  `for-of` is `for _, x := range xs` plus a `_ = x` blank-assign
  (`:4177`); `break`/`continue` are legal in both. No `defer` interaction:
  the emitted `defer`s are mutex- and activation-scoped
  (`:1334`, `:2063`), and Go `defer` is function-scoped, so a `break` never
  fires or skips one. The `RevlFrame` preamble (`:4369`, `:4401`) is
  untouched.
- **rust.** `while cond {}` / `for x in xs {}` (`rust/emit.py:4393-4410`)
  take native `break`/`continue`. The emitted code has no `Drop` impls at
  all (the teardown preamble is `RevlTeardown` + explicit drain,
  `:5094-5264`), so there is no scope-teardown interaction to reason about;
  and `_by_value_tail`'s clone-when-reused iterable logic (`:4406-4407`) is
  unaffected because a `break` does not change how many loops consume the
  binding. The isolate-placement class of emitter bug
  (`docs/design` lineage, cordis-rs is upstream) has no analog here: no new
  items are placed, only a jump emitted.
- **java.** Two statement lowerers emit loops, the v3 fn/test tier
  (`java/emit.py:1686-1697`) and the setup-block tier (`:3352-3362`,
  dispatching on `kind`). Both must gain both cases, or the feature works in
  fns and silently fails to compile in the other tier's contexts. Native
  `break`/`continue` in both; the enhanced-for at `:1691` takes them
  directly.
- **wasm, the hardest tier.** The `while` skeleton is
  `(block (loop <cond> (i32.eqz) (br_if 1) <body> (br 0)))`
  (`wasm/emit.py:4750-4758`); `for` is the same skeleton over an index
  cursor with the increment emitted AFTER the body (`:4826-4827`). Two
  traps, and the design that avoids both:
  1. **Anonymous depth indices break under nesting.** `br 1`-as-break is
     only correct at body top level: every `(if (then ...))` the emitter
     produces (`:4720-4738`) is itself a label, so a `break` one `if` deep
     needs `br 2`, and so on. Rather than threading depth arithmetic
     through `_emit_stmts`, emit **named labels**, which wasm resolves
     regardless of depth and which the emitter already uses in hand-written
     runtime helpers (`$scan` `:588`, `$loop`/`$done` `:2848-2854`,
     `$cont`/`$cont_done` `:3049-3053`, `$__teardown` `:2065`). Skeleton
     per loop, with `N` from the existing `self._loop_counter`
     (`:4668-4674`):
     `(block $revl_brk_N (loop $revl_top_N ... ))`; `break` emits
     `(br $revl_brk_N)`.
  2. **`continue` in `for` must not skip the increment.** A bare branch to
     the loop head would bypass `(local.set idx (i32.add ...))` at `:4826`
     and spin forever. So `for` wraps the body alone in an inner
     `(block $revl_cnt_N ...)`, with the increment after that block and
     before `(br $revl_top_N)`; `continue` emits `(br $revl_cnt_N)`,
     falling out of the inner block into the increment. In `while`, the
     condition test is at the loop head, so `continue` is simply
     `(br $revl_top_N)` and no inner block is needed.
  The emitter tracks the innermost enclosing loop's label pair in a small
  stack alongside `_loop_counter`; `break`/`continue` outside any loop is
  unreachable here (the frontend refused it) but asserts anyway. Validator
  note: code after a `br` is stack-polymorphic and validates, so a
  `break` followed by dead statements is not a wasm error; `_diverges`
  (`:4763-4785`) gains the break-aware `while (true)` rule from the typing
  section so fallthrough values stay correct. The teardown dispatch chain
  and `revl:teardown` section are untouched: loop labels are function-local
  control flow, invisible to `deactivate_step`.

## Self-host (item 391)

Per the standing decision (`docs/v2.0-roadmap.md:4114`), the self-host is a
full current-language compiler, and 391 already names this port slice:
"break/continue (379, parser+lower+teardown+emitters)". After the reference
lands: port the keywords to `selfhost/lexer.rvl`, the statements and
loop-depth refusals to `selfhost/parser.rvl`, the break-aware
`while (true)` rule to `selfhost/checker.rvl`/`lower.rvl`, the step kinds to
every `selfhost/emit_*.rvl` (the wasm one carrying the named-label design
above), and extend the differential corpus so the byte-agreement oracles
(`tests/test_selfhost_*.py`) actually exercise the new steps; a corpus that
never uses `break` leaves the oracle green while the port is missing. The
payoff is the dogfood loop: the self-host's own 18 flag loops then convert,
which is both the cleanup and the bench fixture. This is a follow-up slice
with its own landing, not part of this design's implementation stages; the
new step kinds are additive, so the oracle stays green on the existing
corpus family until the port lands (new IR shapes appear only in programs
that use the feature).

## Staged implementation plan

Each stage lands independently and keeps the suite green; per the standing
gate, an `emit.py` change is verified against the per-backend goldens, which
`pytest tests/` does not run.

- **Stage 1: lex + parse + loop-only validation.** Keywords in
  `lexer.py`/`formatter.py`; `BreakStmt`/`ContinueStmt`; the parser loop-depth
  counter; the three refusals (fn-grammar depth-0, component/method-grammar
  redirect, and the for/while body positions accepting them at any depth).
  Harness-corpus identifier sweep first. Exit: refusal messages as specced;
  `break` inside `if` inside `while` parses; every existing test green;
  parse of any no-break program byte-identical.
- **Stage 2: flow analysis.** The break-aware `while (true)` clause in
  `_definitely_returns` (`lower.py:958`), docstring rewritten to name the
  rule instead of the absence. Exit: a declared-return fn ending in
  `while (true) { break }` is refused for a missing return; one ending in
  `while (true) { }` stays accepted; `_has_return` behavior unchanged.
- **Stage 3: lower + the invariant guard.** The two step kinds in
  `_lower_pure_stmt`; the registering-step-in-loop-body assertion
  (Decision 2); `docs/syntax-2.0.md` §3.5 gains the two statements and the
  TS-verbatim `continue` note. Exit: IR goldens for a break/continue
  program; byte-identical IR without them; the guard assertion has a test
  proving it fires on a synthetic registering step.
- **Stage 4: emit, py + ts first, then go/rust/java, then wasm.** Native
  statements on five tiers (both java lowerers); the wasm named-label
  skeleton, inner `for` continue-block, and `_diverges` update. Exit:
  per-backend goldens for a matrix of {while, for} x {break, continue,
  nested-if break, break-then-code-after-loop}; byte-identity of every
  existing golden on all six tiers (the wasm skeleton change must be gated
  to loops that contain a `break`/`continue`, or the label-only diff must be
  taken consciously as a one-time golden regeneration, decided at
  implementation with the no-break byte-identity exit test as the
  arbiter: the cheapest sound choice is to emit named labels only when the
  loop body contains a `break`/`continue`, keeping every existing golden
  byte-stable).
- **Stage 5: soundness exit tests + bench gate.** The teardown composition
  tests below, executed on the py tier (the reference runtime) and mirrored
  in the tck scenario shape where the tiers execute; the selfhost-lexer
  bench comparing a flag loop against its `break` rewrite, landing the
  number in the roadmap item (the perf claim is not made without it).
- **Stage 6 (separate slice, 391): the self-host port**, as scoped above.

## Exit tests

Parsing and refusals:

- `break`/`continue` at loop-body top level and nested under `if` parse in
  fn and test bodies; outside a loop they refuse with the redirect (not G1);
  in an activation or provide-method body they refuse with the no-loops
  redirect; `let break = 1` refuses as a keyword collision.

Flow:

- declared-return fn with `while (true) { if (c) { break } }` and no return
  after the loop: refused (missing return); the same fn with a `return`
  after the loop: accepted; `while (true)` with no break: accepted with no
  trailing return, as today.

Soundness (the reason this was design-first), all on the reachable
composition (activation registers, called emitting fn loops):

- **acquire released exactly once across `break`.** An activation body
  `let h = effect open() undo close(h)`, then calls a fn whose `while` loop
  `break`s mid-iteration; on clean withdrawal `close` runs exactly once, at
  activation teardown, not at `break` time; the trace shows no teardown
  between loop exit and the activation boundary.
- **compensation neither double-run nor skipped across `continue`/`break`.**
  Activation body performs `emit db.insert(row) compensate db.delete(id)`,
  then calls a fn whose loop `continue`s and later `break`s. Clean commit:
  the compensation is discharged, never run (the row survives). Abort after
  the call returns: Phase 1 completes, then the compensation runs exactly
  once in Phase 2. Both traces identical to the header-flag-loop version of
  the same program.
- **witnessed loop that breaks.** A witnessed acquisition in the activation,
  a breaking loop in a called fn, then an abort: the witnessed inverse
  replays exactly once in Phase 1; on commit it discharges with the WAL
  discharge record. Byte-compare the residue envelope against the flag-loop
  twin.
- **abort raised inside the loop.** The fn's loop body itself raises (a
  `fail` propagated to A8) on iteration 2 of 5: the activation frame drains
  both phases completely, LIFO over everything registered before the raise;
  the loop's partial progress is invisible in the drain order.
- **the invariant guard.** A synthetically-constructed IR with an `effect`
  step inside a `while` body trips the Stage 3 assertion with a message
  naming this doc.

Byte-identity and portability:

- every program without `break`/`continue` is byte-identical across parse,
  IR, and all six backend goldens (including wasm, per the Stage 4 gating);
- the break/continue golden matrix compiles and runs on all six tiers; on
  wasm, an executed live-module test (the `lifecycle.py` harness precedent)
  proves a `for` with `continue` visits every remaining element exactly once
  (the increment is not skipped) and a `break` nested two `if`s deep exits
  exactly one loop.

Docs:

- `test_doc_examples` stays green: this note contains no revl syntax blocks
  that must not compile (worked examples land with Stage 3's syntax-2.0
  edit, where they compile).

## Honest notes

- The frame-neutrality theorem is only as strong as the grammar
  disjointness, which is why Decision 2 makes it an executable assertion
  rather than a comment. The day loops and registration meet, that item owes
  the per-iteration semantics and the wasm story; this design deliberately
  does not pre-decide them, because no measured want exists yet and the
  wrong guess would be load-bearing.
- The wasm golden question in Stage 4 (labels only-when-needed vs a one-time
  regeneration) is left to the implementation agent with the exit test as
  arbiter; both are sound, one is byte-stable.
- The perf claim stays bench-gated. If the selfhost-lexer number comes back
  flat, the item still pays for itself on the first two costs, and the
  roadmap entry should say so rather than carry an unverified speedup.
- Labeled `break` is explicitly out of scope; the refusal message should not
  promise it.

## Fable review corrections

An adversarial review of this note found four issues. They are authoritative
and override the body above where they differ; the implementation lands them.

- **C1 (correctness, must-fix).** A `break`/`continue` inside a `match`-BLOCK
  ARM inside a loop must be REFUSED. A block arm is lambda-lifted into a
  separate helper `fn` during lowering (`src/revl/lower.py`, `_lift_block_arm`),
  so a `break` written there would land in a function with no loop and emit
  broken artifacts on every tier — the same reason the codebase already refuses
  `return` in a block arm. The parser resets its loop-depth context to 0 on
  entering `_match_block_arm` (so the counter cannot see an enclosing loop
  through the lift) and refuses `break`/`continue` there in the block-arm voice.
  A loop written *inside* the arm restores a positive depth for its own `break`.
  Covered by an exit test.
- **C2 (enforcement).** The no-loop-scoped-registration invariant is enforced
  as a WHOLE-IR VALIDATION PASS (`_validate_no_loop_scoped_registration`,
  `src/revl/lower.py`), run once over the lowered IR: no registering step kind
  (`effect`/`let-effect`, `emit`/compensate, `timer`, `approval`, `spawn`) may
  appear inside any `while`/`for` body, and — the same invariant read the other
  way — no `while`/`for` step may appear in a component activation, provide-
  method, or setup body. A parse-time loop-depth counter or a single
  `_lower_pure_stmt`-local assert is not enough: the java emitter's
  `_emit_setup_stmt` already emits loop steps in activation-body position, so a
  leak on that path would compile silently on one tier. Each emitter's loop case
  also carries a cheap belt-and-suspenders guard.
- **C3 (premise reword).** The theorem's premise "the only scope that acquires
  is the activation" is false — a bare `acquire`+`undo` extern is callable from
  a fn loop today (separately filed as bug item 399, out of scope here). The
  premise is reworded to **"the only scope that REGISTERS teardown is the
  activation."** Frame-neutrality is unaffected: a bare `acquire` from a fn loop
  registers nothing, so an early loop exit still runs no teardown.
- **C4 (couples bug 398, wasm terminates-check).** The wasm `_diverges`
  (`backends/wasm/emit.py`) handled only `return`/`if` and lacked a
  `while (true)` case, while the frontend `_definitely_returns` treated
  `while (true)` as terminating, so a declared-return fn ending in
  `while (true)` emitted wasm wasmtime rejects ("nothing on stack"). Filed as
  bug 398 but coupled here: the wasm terminates-check is made BREAK-AWARE and
  the missing `while (true)` case is added to `_diverges` at the same time, so
  both judgments agree — a `while (true)` with no reachable `break` diverges
  (terminates the fn); one with a reachable `break` does not. A wasmtime-
  validated exit test covers a declared-return fn ending in `while (true)` both
  with and without a break.
