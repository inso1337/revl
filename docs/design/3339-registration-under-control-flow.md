# 3339: a teardown-registering step under `if`, `while` or `for` in a provide method

**Roadmap:** no item (the deferred remainder of design note 478 items 8 and 9,
which the issue #548 control-flow slice left "to its own note") · **Issue:**
#3339 · **Status:** DESIGN, no code in this note · **Kin:** 478
(`docs/design/478-component-author-ergonomics.md` §Group 3 items 8, 9), 379
(`docs/design/379-break-continue.md`, the frame-neutrality theorem),
`docs/design/teardown-contract.md` (the one LIFO stack), 243 (Ok-conditional
registration), 397 (result-guarded undo), #3304 (per-call record growth),
#3340 (the looped `emit` rule, open, not decided here).

## The problem, as the compiler states it

A component that owns a store writes one entry per call and only
unconditionally. Each of the issue's three bodies is refused at lowering
(`src/revl/lower.py:15730`, `_lower_control_stmt`), measured on this tree at
`2a940d9e1` with the four programs of #3339 (control compiles; `if`, `while`
and `for` refused):

```
a teardown-registering step (`effect`/`let-effect`/`await`) is not allowed
inside a provide-method `if`/`while`/`for` body
  the activation frame owns every inverse and registers it at the method's top
  level; a conditional or looped acquisition would need the teardown contract
  amended (issue #548, docs/design/478-component-author-ergonomics.md §Group 3).
```

The two customers (the live game studio's version store and its frame ring,
#3339 §Where the playground meets it) need a conditional write, a batch write
and a batch drop, each in one call. Today the control flow may compute values
and fire bare `emit` crossings (item 458), nothing more.

Note 478 item 8 states the bound the #548 slice chose ("the arms are PURE",
`src/revl/lower.py:15433`) and item 9 states the loop half ("a registering
step still may not sit inside a loop body", enforced whole-IR by
`_validate_no_loop_scoped_registration`, `src/revl/lower.py:8462`). Note 379
lists what this note has to decide before loops and registration meet
(`docs/design/379-break-continue.md:223-228`): per-iteration accumulation,
frame growth, entry provenance, the wasm static chain, and an amendment to
`teardown-contract.md`. The three sections below answer the owner's three
questions in that order: the teardown contract, frame growth, and `break` and
`continue`.

## 1. What a registration per branch or per iteration means for the teardown contract

### The contract already has one answer: registration happens where the step runs

The contract keeps one LIFO disposer stack per activation; the three entry
kinds "interleave freely in registration order, and both teardown phases walk
the same stack" (`docs/design/teardown-contract.md:44-47`). The registration
row of its table (`docs/design/teardown-contract.md:58`) says WHEN each kind
registers: a bracket "at acquisition", a transactional entry "on the `Ok`
branch only (243)", a compensation "at the `emit`". Nothing in the table or the
algorithm (`docs/design/teardown-contract.md:61-112`) counts entries per
method or per source line. The stack is a runtime list in registration order,
and the drain is `reverse(stack)`.

Two registrations in the tree are already conditional on a runtime value, so
"registered only when the step ran" is not a new idea in the contract:

- A witnessed effect registers its declared inverse on the `Ok` branch and
  "on `Err`, it registers nothing" (`docs/design/243-witnessed-externs.md:158-163`);
  the method-body form registers from inside a method call, per tool call
  (`backends/python/emit.py:3883`, `_method_witnessed_step`).
- A `let ok = effect m.insert_if_absent(k, v) undo m.remove(k)` registers a
  result-guarded inverse: "the emitters register the site-spelled undo ONLY when
  the bound result is true" (`docs/design/397-insert-if-absent.md:367-372`;
  py: `backends/python/emit.py:4336-4340`, the `undo if bind else None` head).

A plain method-body bracket is emitted as one runtime effect per executed
step: the py tier wraps the acquisition in a generator whose yield is the
guarded inverse and adopts it onto the component frame
(`backends/python/emit.py:4296-4315`, `_revl_frame.adopt(_revl_ctx.effect(...))`);
`Frame.adopt` appends to `Frame._adopted` (`backends/python/runtime.py:4442-4445`)
and only `Frame._dispose_adopted` walks that list, `reversed`, continue-and-
record (`backends/python/runtime.py:5161-5180`). The runtime therefore already
registers "once per time the step ran", with the method's top level being the
only place the grammar lets the step run.

**Decision 1.1 (the rule).** A teardown-registering step registers exactly when
and where it executes: once per executed `if` arm, once per executed loop
iteration, in execution order, onto the SAME activation stack. There is no
branch scope and no iteration scope on the frame. An untaken branch and a
skipped iteration register nothing. This is the registration row of the
contract table read literally, plus one sentence the amendment adds (§1.3).

**Decision 1.2 (discharge and replay).** Unchanged: clean unload runs bracket
inverses LIFO and discharges the rest; abort replays LIFO over everything
registered up to the abort point, compensations in Phase 2
(`docs/design/teardown-contract.md:61-112`). For a loop of N executed
iterations that means N entries replayed newest first, so iteration N's
inverse runs before iteration 1's. For the issue's `for (i of [0, 1, 2])`
body that is `m.remove(k + 2)`, `m.remove(k + 1)`, `m.remove(k)`.

### Provenance: the label stays per site, the arguments are per registration

Each py entry carries the step's label (`backends/python/emit.py:4315`,
`self._label(label)`), and a Phase-1 failure is recorded against that effect
(`backends/python/runtime.py:5171-5179`, `_record_phase1_residue`). Inside a
loop every iteration shares one label; what distinguishes the entries is the
inverse closure's captured arguments (`k + i` is evaluated at registration,
the inverse lambda closes over the method locals of that iteration) and the
stack position. **Decision 1.3:** provenance is label plus captured arguments
plus registration order; the slices add no per-iteration label and no
iteration index to the residue schema (`docs/design/teardown-contract.md:504`,
"246 freezes this"). A residue record from iteration 2 reads exactly like a
second top-level call's would. (Read in the py emitter and runtime; the other
tiers' label handling was not re-read for this note and the loop slice must
confirm it.)

### Failure in iteration k

Registration is at acquisition (`docs/design/teardown-contract.md:58`), and
on py the generator runs the acquisition before it yields the inverse
(`backends/python/emit.py:4296-4313`), so an acquisition that raises registers
nothing for its own iteration. Iterations 1..k-1 are on the frame; the raise
leaves the method; nothing on the frame moves at that moment, because a method
raise is not a teardown boundary (the frame's adopted list is touched only by
`_dispose_adopted`, `backends/python/runtime.py:5161`). The k-1 entries are
then discharged or replayed by whatever ends the frame: commit, abort, unload
or swap. Whether the caller's error aborts the activation or only fails the
call is the runtime's existing policy for a raising method, unchanged by this
note (inferred from the adopt/dispose code above, not read in the call path).
The loop slice's test (§4) pins "k-1 registered, k-1 replayed, in reverse
order" on each tier that runs method brackets.

### The amendment to `teardown-contract.md` (one paragraph, landed by the `if` slice)

Under "The three entry kinds, one stack", after the table:

> A step that registers (a bracket, a witnessed or result-guarded acquisition,
> a compensation) may sit under `if`, `while` or `for` in a provide-method body
> (issue #3339). It registers each time it executes, in execution order, on
> this stack; an untaken arm or a skipped iteration registers nothing; a loop
> of N executed iterations pushes N entries. No branch or iteration scope
> exists on the frame: `break`, `continue` and the end of an iteration run no
> teardown (`379-break-continue.md`, Decision 1), and the drain stays
> `reverse(stack)`.

### Tier qualifications

- py, ts, rust, go, java render a plain method-body bracket as a dynamic
  per-execution registration (py `backends/python/emit.py:4296-4315`; ts
  `backends/typescript/emit.py:1848` is the method `effect`/`let-effect` arm;
  rust `backends/rust/emit.py:5352`; go `backends/go/emit.py:2424`,
  `ctx.Effect(...)`; java `backends/java/emit.py:8472`). The `if`/`while`/`for`
  method renderers on every tier already call the method step renderer for the
  arm bodies (py `backends/python/emit.py:4425-4462`, `_method_control`,
  "Byte-for-byte the fn-grammar shape"), so an `effect` step placed inside an
  arm's step list renders where the arm renders. The slices must still run
  each tier's golden and its runtime test; this note read the py path and the
  entry points of the others.
- wasm refuses every plain method-time effect today, control flow or not:
  "method-time effects are not lowerable, the wasm accumulator is fixed at
  activation (state machine)" (`backends/wasm/emit.py:2512-2516`). The issue's
  programs also use `Map.new`, which that tier has no representation for
  (`backends/wasm/emit.py:37-39`). **Decision 1.4:** on wasm the three bodies
  stay refused for good, by the tier's own existing message, and the `if`
  and loop slices change nothing there. The 379 "wasm static-chain gap"
  (`docs/design/379-break-continue.md:194-199`) therefore does not block the
  slices: the static `revl:teardown` section indexes activation-time entries
  only, and the only method-time registration wasm admits, a WITNESSED effect
  into the runtime `$__mw_*` cells (`backends/wasm/emit.py:2035-2045`, "one
  cell per `deactivate_step` call"), is dynamic by construction. A witnessed
  effect under a method loop on wasm is representable in those cells but is
  not asked for by #3339 and is left as a named remainder (§5).

## 2. Frame growth

One registration per iteration grows the frame by one entry per iteration.
The question is whether that is a new growth class or the one #3304 already
measures.

#3304 measured the per-call case on py: 504 `FiberEffect` objects alive after
500 `tick` calls, about 4.3 KB retained per call, swap time close to linear in
the record count (0.31 s after 1000 ticks). The owner's direction on #3304
(comment of 2026-11, read at `origin/main` 49615bbec): coalesce at the
runtime, using the per-frame `(map, key)` journal that `_journal_note` keeps
(`backends/python/runtime.py:8122-8131`, `setdefault` of the prior value): a
bracket whose forward write journals a `(map, key)` the frame already journaled
and whose inverse is a `table` inverse is released without running, so "the
bound becomes one record per distinct key per frame".

**Decision 2.1 (no new class).** A loop body that registers N times in one
call pushes exactly what N top-level calls push today: the same step kind, the
same `adopt`, the same label. Nothing in the slices changes what one entry
costs. The growth is #3304's growth, reached in fewer calls.

**Decision 2.2 (the bound is #3304's, applied as is).** With #3304 slice 1
landed, a loop that writes the same key N times coalesces to one record, and a
loop that writes N distinct keys keeps N records, which is the information the
frame has to hold to revert N distinct writes. The issue's batch customer
(`put(i, g)` for `i` in a range) is the second case: its record count equals
its distinct key count whether the writes come as N calls or one looped call.
The slices here do not depend on #3304 landing first, and #3304 does not depend
on them; the loop slice carries a named-count test (§4) so the two can be
sequenced either way with the numbers visible.

**What is new in degree.** `while (i < v)` makes the number of registrations
per call a caller-supplied value. Today the worst a caller can do is one
registration per call; after the loop slice one call can push `v` entries.
This is the one point the note cannot settle from the code, so it is open
question Q3 (§6): no bound, a runtime per-call ceiling, or a static iterable.
The recommended default is no compiler bound, because the frame's growth is
already caller-paced (a caller may call `put` `v` times today) and #3304's
runtime coalescing is where the bound lives.

## 3. `break` and `continue`

Note 379 decided this before the grammar admitted the case, and wrote the
sentence for the future registering form explicitly
(`docs/design/379-break-continue.md:139-170`):

- Decision 1: "no revl teardown boundary coincides with a loop boundary";
  `break`, `continue`, early `return` and end-of-iteration "run exactly the
  same amount of frame teardown: none."
- `continue`: "Any entries a (future) registering form had pushed before the
  `continue` stay on the frame in registration order."
- `break`: "Statements after the loop see the frame exactly as a header-flag
  exit would have left it."
- abort through a loop: "Loop structure is invisible to the drain."

Note 379 also proves the alternative, iteration-scoped teardown, unsound on
three grounds (`docs/design/379-break-continue.md:172-199`): it fires
abort-only compensations on a clean path (the 247 bug class), it splits the
LIFO stack and the WAL `seq` order, and wasm cannot represent it.

**Decision 3.1.** The loop slice adopts 379 Decision 1 unchanged: `break` and
`continue` are admitted in a loop that registers, and they touch the frame in
no direction. A `break` after iteration j leaves j entries; a `continue` that
skips the registering step in iteration j leaves that iteration unregistered
and the next iteration registers as usual. 379's own words become the
contract paragraph (§1.3). Refusing `break`/`continue` in a registering loop is
not needed and would refuse the realistic batch shape (`for` over a list,
`break` on the first miss).

**Decision 3.2 (what the invariant becomes).** 379 Decision 2 made "a
registering step inside a loop body" a refusal at lowering
(`src/revl/lower.py:8462`, `_validate_no_loop_scoped_registration`, and the
component half at `src/revl/lower.py:8504-8516`) and in every tier's fn-body
renderer (py `backends/python/emit.py:5415`, `_guard_frame_neutral_loop`).
The theorem that guard protects, "loops are frame-neutral because loops and
registration never meet", is replaced for provide-method loops by the
stronger statement above: loops are frame-neutral because NO loop control
transfer touches the frame, registration or not. The fn-body half of the guard
stays (a module `fn` has no frame to register on; the grammar keeps it
unreachable), the method half is relaxed by the loop slice, and the per-tier
fn-body guards stay as they are. The loop slice must check per tier whether a
frame-neutral guard is wired into the METHOD loop renderer (on py it is not:
`_method_control` renders arms through `_emit_block`,
`backends/python/emit.py:4437-4457`, without calling `_guard_frame_neutral_loop`).

## 4. The slices

### Slice 1: `if` alone

What changes:

- `src/revl/lower.py` `_lower_control_stmt` (`:15609`): the `LetEffect`/
  `EffectStmt` arm of the refusal at `:15730` lowers the step instead, through
  the same `_lower_effect_step` the method top level uses
  (`src/revl/lower.py:15819-15846`), appending the `{"step": "effect", ...}`
  or `{"step": "let-effect", ...}` to the ARM's step list (`then_body` /
  `else_body`, `:15617-15627`), when `loop_depth[0] == 0`. Under a loop the
  refusal stays, with a message that names this note and the loop slice.
  `AwaitStmt`, `LetApprovalStmt` and `TimerStmt` stay refused in that arm (not
  asked for; see Q1). `_lower_scoped_block` (`:15585`) already gives a
  `let x = effect ...` bind arm-local visibility with a method-unique emitted
  name, so a bind in one arm cannot alias one in a sibling arm.
- No emitter change is expected on py, ts, rust, go, java: the method control
  renderer hands arm steps to the method step renderer (§1.4). The slice
  verifies that by golden on each tier and fixes any tier whose method `if`
  renderer special-cases the arm's step kinds. wasm: no change (Decision 1.4).
- `_validate_no_loop_scoped_registration`: no change (an `if` is not a loop).
- Self-host checker: `selfhost/lower.rvl` `es_flow_block_verd` (`:26541`)
  refuses the first registering head inside any `if`/`while`/`for` block; it
  narrows to loop blocks (and to `await` heads), and `es_teardown_in_flow_msg`
  (`:26522`) is re-worded with the reference's new text so the byte-agreement
  oracle (`tests/test_selfhost_lower.py`) stays green. The fixture rules
  `tests/fixtures/effect_statement_rules/t1_effect_in_method_if.rvl` and
  `t1_effect_in_method_else.rvl` become `ok_*` fixtures; `t1_*_while.rvl`,
  `t1_let_effect_in_method_for.rvl` and `t1_verified_effect_in_method_if.rvl`
  stay T1 (Q2 for the verified one).
- Tests to flip: `tests/test_548_control_flow_in_provide_methods.py::test_effect_inside_control_flow_is_still_refused`
  (`:459`) becomes the positive test; `tests/test_method_effect_computed_inverse_2640.py`,
  `tests/test_gate_reference_census.py` and the
  `tests/fixtures/selfhost_uncovered_lines/*/ts.jsonl` fixtures quote the old
  message and are regenerated or re-pointed with the repo tools, never
  hand-edited.
- `docs/design/teardown-contract.md`: the §1.3 paragraph. `docs/design/478-component-author-ergonomics.md`
  items 8 and 9: the REMAINDER sentence points here (this PR does that).

What the `if` slice means for the issue's program:

- Where the inverse registers: in the arm, at the step, on the component's
  activation frame, as a plain bracket (same `adopt` as the top level).
- What an untaken branch leaves: nothing. `put(1, 0)` runs the condition,
  skips the arm, registers no entry, writes no key.
- What undo of the method does: `put(1, 5)` adopts one bracket whose inverse is
  `m.remove(1)`; abort or `undo` replays it in Phase 1 (bracket row of the
  table), the #1945 journal checks key `1` returns to its prior (absent), and
  commit discharges it (a bracket's inverse runs on clean unload too,
  `docs/design/teardown-contract.md:52`, so a clean unload also removes the
  entry, exactly as the top-level `put` does today).

Acceptance (the brief's): the issue's `if` program compiles on py, ts, rust,
go, java (refused on wasm by its own message); on the py runtime `put(1, 0)`
leaves the frame's adopted list and the map unchanged, `put(1, 5)` leaves one
adopted entry and `m.get(1) == Some(5)`, and undo of the call removes the
entry and the key with no residue. The fixture oracle, the census test and the
selfhost byte-agreement tests stay green with named counts.

### Slice 2: `while` and `for`

- `src/revl/lower.py`: lift the `loop_depth[0] > 0` half of the slice-1
  refusal; relax the component half of `_validate_no_loop_scoped_registration`
  (`:8504-8516`) for `effect`/`let-effect` steps under a provide-method loop,
  keeping it for the fn-body half and for `emit`/`timer`/`approval`/`spawn`
  until Q1 and #3340 are answered.
- Per tier: confirm the method loop renderer renders the registering step in
  the body (§3.2's check); fix the tier whose runtime cannot register from a
  loop body, if any (none expected on py, ts, rust, go, java; wasm unchanged).
- Self-host: `es_flow_block_verd` narrows further to `await` heads only, or is
  removed with its fixtures.
- Tests: a looped `effect` registers N inverses and a teardown discharges them
  in reverse order (assert the order of the `remove` calls); a failure in
  iteration k leaves k-1 inverses registered and replayed in reverse order;
  `break` after iteration j leaves j entries; `continue` skipping the
  registering step in iteration j leaves N-1 entries; the issue's `while` and
  `for` programs compile on every tier that runs method brackets; a
  named-count frame-growth test (a loop of N distinct keys adopts N entries,
  the same N as N calls, measured on py with `FiberEffect` counts as #3304
  does).
- `teardown-contract.md`: nothing beyond the §1.3 paragraph, which already
  covers iterations.

Open points for this slice: Q3 (a bound on the trip count), and the
interaction with #3340 below.

### Slice 3, conditional on #3340: `emit ... compensate` under control flow

`_lower_control_stmt` refuses a compensated `emit` in an arm separately
(`src/revl/lower.py:15718-15728`, "a CONDITIONAL one needs the teardown
contract amended") and any `emit` in a method loop (`:15708-15716`). #3340
shows the loop refusal follows the statement spelling only: `let r = emit ext(i)`
in a `while` compiles and registers one compensation per iteration. The owner
asked #3340 to state which way the rule goes before any code. This note states
only how the two meet, and decides neither:

- If #3340 lands "every looped `emit` is refused", slice 2 admits brackets in
  loops and keeps every `emit` kind out of them, in both spellings; a
  compensation under `if` (not a loop) is still open and is this slice's whole
  content.
- If #3340 lands "a bare crossing may be looped in both forms", a compensated
  crossing in a loop is exactly a registration per iteration and falls under
  Decisions 1.1 to 1.3 here: one compensation entry per executed iteration,
  Phase 2 LIFO on abort, discharged on commit, no iteration scope. Slice 3
  then admits `emit ... compensate` under `if` and in loops together, with the
  teardown paragraph of §1.3 already covering it ("a compensation" is named
  in the paragraph for that reason).

Either way the compensated case must not stay accepted in one spelling only
(#3340's acceptance), which is why slice 3 waits for #3340 rather than the
reverse.

## 5. Remainders named, not scheduled

- `await`, timer and approval statements under method control flow: still
  refused after the three slices; they register other entry kinds and were
  not asked for.
- A WITNESSED effect under a method loop on wasm: representable in the runtime
  `$__mw_*` cells (§1.4), not covered by the slices.
- `verified` effect under control flow (Q2).
- The per-record CPU cost (the cordis stack capture) is #3390's, not this
  note's.

## 6. Decisions for the owner

Each has a recommended default; a choice nothing in the code or docs settles.

**Q1. Which registering statement forms does slice 1 admit in an arm?**
(a) `effect` only; (b) `effect` and `let ... = effect` (the two forms the
method top level admits today, including the spawn handle and the result-
guarded CAS of 397); (c) also `emit ... compensate`. Recommended: (b). The CAS
form is already result-guarded, so admitting it conditionally stacks two
guards that the emitted shape handles; (c) belongs with #3340 (slice 3).

**Q2. A `verified` effect under control flow.** Issue #1897 made the round
trip of a verified method effect "activate, call the method, abort, compare"
(`src/revl/lower.py:15847-15850`). Under `if` the branch may not run, so the
round trip has nothing to compare. (a) keep `verified` refused under control
flow (the fixture `t1_verified_effect_in_method_if.rvl` stays T1); (b) admit
it and define the round trip as skipped when the branch did not run.
Recommended: (a) for slices 1 and 2; (b) is its own small note if a customer
asks.

**Q3. A bound on registrations per call in a loop.** (a) none: the frame
grows as #3304 measures and #3304's runtime coalescing is the bound; (b) a
runtime per-call registration ceiling (host configuration, the call fails past
it, like the Phase-2 budget variables of `teardown-contract.md`); (c) admit
only a `for` over a list in a registering loop, refuse `while`. Recommended:
(a), with slice 2's named-count test making the growth visible; (b) can be
added later without changing the contract, (c) refuses the issue's `while`
customer.
