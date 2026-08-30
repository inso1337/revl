# 386: report ALL refusals per compile, not just the first

Design note for roadmap item 386 (`docs/v2.0-roadmap.md:4104`). It records how
the compiler fails-fast on the first refusal today, a collect-and-continue
recovery model, how to recover past a refusal without emitting cascading false
errors, the per-file vs per-compile scope decision, the output shape, and a
staged plan an implementation agent can pick up.

## Stage 1 as built (Fable review corrections — authoritative for Stage 1)

Stage 1 was reviewed before implementation and four corrections were applied.
Where this section differs from the sink/Poison prose below, THIS section
governs Stage 1; the sink and `Poison` machinery are **deferred to Stage 2**.

- **Change 1 — header-stub, don't drop (soundness).** When a component's BODY
  lowering aborts, appending "no lowered component and continue" corrupts
  `_link`: the multi-realm route check (`lower.py`) fabricates "no component
  provides key in realm" for consumers routing to the refused component's
  realm, and G2 (`provider_of`) / G3 (cycle DFS) silently MISS a real
  conflict/cycle on a key it declares. Instead append a HEADER-ONLY stub
  (`provides`/`requires` from the `component` declaration — available before
  body lowering — marked `poisoned: true`) so `_link`'s topology is complete,
  and EXCLUDE poisoned stubs from the body-walking post-passes (`check_taint`,
  spawn bounds/attenuation, holes). `isolate`/`routes` are body statements, so a
  stub carries none: its provisions sit in the shared realm, which is exactly
  enough to stop the route-check fabrication and keep G2/G3 sound over its keys.
  See `_component_header_stub` in `lower.py`.
- **Change 2 — Stage 1 needs NO sink threading.** Do not thread a sink through
  every `_lower_*` helper or convert every `raise`. Instead: (1a) wrap each
  iteration of the component loop (including the A1 reach raise) in
  `try/except RevlError: errors.append(e); <append header stub>; continue`;
  (1b) wrap each post-pass (`check_taint`, handoff, spawn bounds, attenuation,
  fault tests, `_link`, tests) in a `_collect` helper so each runs even when
  `errors` is non-empty; (1c) convert `_link` INTERNALLY to collect-all via an
  optional `errors` sink (G2 loop, route loop, cycle DFS — but CAP at ONE
  reported cycle per SCC to avoid overlapping-path noise). Phase-2 table
  failures (types/signatures, BEFORE the loop) keep RAISING = the truncation
  behavior; there is no `fatal()`/`Poison` machinery in Stage 1.
- **Change 3 — one carrier, ~70 catch sites unchanged.** `check_and_lower`
  raises a `RevlErrors(RevlError)` carrier when `errors` is non-empty, whose
  primary fields (filename/line/message/code/…) MIRROR THE FIRST error so
  `classify()` and every legacy single-error `except RevlError` consumer behave
  exactly as today. `RevlErrors.__str__` renders the full list plus a census
  line (so the many `print(f"error: {error}")` sites upgrade for free; a lone
  refusal renders byte-identically). `report()` (`diagnostics.py`), `plan._add`
  (`plan.py`) and the LSP `compute_diagnostics` (`lsp/analysis.py`) iterate the
  carrier's `.errors` list; a plain `RevlError` (no `.errors`) still yields a
  one-element list. Note `plan._add`'s dedup key EXCLUDES filename by design
  (the gate abspaths, the standalone compile does not); the carrier's own dedup
  key INCLUDES filename.
- **Change 4 — stable order, guarded crashes.** Diagnostics are deduped on
  `(code, filename, line, message)` and sorted by COMPILE ORDER — `(file
  position in the compile-args order, line)`, not alphabetical — so
  `diagnostics[0]` equals what today's single-error compile reports for the same
  input (Python's stable sort keeps append/pipeline order on ties). And a
  post-pass that throws an UNEXPECTED (non-`RevlError`) exception on poisoned
  state is DROPPED while the compile is already failing, so a stray `TypeError`
  cannot replace N good diagnostics with a traceback; when `errors` is empty the
  crash propagates as the bug it is.

## Why this is the velocity lever

The compiler reports exactly one refusal per compile: the first `RevlError`
raised anywhere in the frontend aborts the whole pipeline. For a human this is a
mild annoyance. For an LLM authoring revl it is the dominant cost. Every refusal
is a full round-trip: emit the program, read the one error, fix it, recompile,
hit the next one. H38 (revl-harness) needed six full 29-composition passes to
discover 62 offending sites, and what actually worked was abandoning the
compiler as an oracle and writing a crude static pre-scan with the compiler
demoted to a verifier. Item 386 is the language telling the author everything it
knows in one pass instead of leaking it one round-trip at a time.

This serves the same goal as the exclusion diagnostics in `docs/syntax-2.0.md`
§0 and §10. §0's corollary is that TS constructs revl does not share are
"excluded and named in diagnostics"; §10's claim is that these diagnostics
"convert wrong priors into one-shot corrections." A one-shot correction that
only surfaces one of six wrong priors per compile is six-shot in practice. Item
386 is what makes the exclusion-diagnostic philosophy actually one-shot: every
excluded construct in a file named in a single pass.

## The current shape: one abort type, one catch, one-element list

The frontend has exactly one abort mechanism and no diagnostic accumulator
anywhere in the parse, typecheck, or lower path.

### One exception class

`RevlError` (`src/revl/errors.py:12`) is the single abort type raised by lexer,
parser, typecheck, lower, compiler, and the admission gates. Its fields
(`errors.py:13-31`):

- `filename`, `line` for location. Location is a bare one-based line integer;
  there is no column. The LSP confirms it: "a RevlError carries a one-based line
  but no column" (`src/revl/lsp/analysis.py:76-77`).
- `message`, and an optional `hint` fix line.
- `code`, `category`: optional structured tags for the agent projection.
- `expected`, `actual`: an optional type-mismatch pair.
- `why`: an optional `WhyTrace` (the G4/G3/G2 derivation).

The constructor eagerly renders `filename:line: message` plus hint and trace into
the `Exception` args (`errors.py:32-38`).

### One projection, hardcoded to one element

`src/revl/diagnostics.py` is purely a projection of a single `RevlError`:

- `classify(error)` returns one record dict (`diagnostics.py:135-176`), deriving
  `code`/`category` from an explicit field, else a `(G#/A#)` tag in the message,
  else a message-shape table.
- `report(error)` returns `{"ok": False, "diagnostics": [classify(error)]}`
  (`diagnostics.py:204-206`). The list is hardcoded to exactly one element. This
  is the seam item 386 changes.
- `explain`, `GUARANTEES`, `FIXES` (`diagnostics.py:24-103`) enumerate G1-G9,
  A1-A9, T1-T3 and the fix redirects. This is the exclusion-diagnostic
  vocabulary the output must carry per refusal.

### One catch, exit code 1

The CLI compile step (`src/revl/__main__.py:602-609`):

```
try:
    ir = compile_files(args.files)
except RevlError as error:
    if getattr(args, "json_diagnostics", False):
        print(json.dumps(report(error), indent=2))
    else:
        print(f"error: {error}", file=sys.stderr)
    return 1
```

One `except RevlError`, exit code 1, catching only the first raise. A
machine-readable mode already exists (`--json` sets `json_diagnostics`) and emits
`report(error)`, that same one-element list.

### The precedent already in the repo

Three surfaces already accumulate-then-report, which is exactly the shape item
386 generalizes:

- Typed holes: `holes.collect(ir)` gathers every open hole into a sorted list
  (`src/revl/holes.py:27-48`), and `__main__.py:614-624` prints the full list
  after a successful compile. This is the working "check all, report all" model
  in-repo, and it already has a JSON form via `obligations(holes)`
  (`diagnostics.py:179-201`).
- Policy: `policy.evaluate(...)` returns a `list[Violation]`; `first_error`
  raises only `violations[0]` (`src/revl/policy.py:655-665`) while
  `render_report` renders all of them (`policy.py:677`).
- LSP: `compute_diagnostics` already returns a `list[dict]`
  (`src/revl/lsp/analysis.py:41-53`) and documents that "slice 1 publishes at
  most one diagnostic" because "the checker stops at its first rejection." This
  consumer is pre-shaped to grow to N with zero client change.

The design below is not inventing a diagnostics container. It is extending the
holes/policy pattern from those local surfaces to the whole frontend.

## The compile spine and its natural boundaries

`check_and_lower(program, ambient)` (`src/revl/lower.py:3403`) is the frontend
spine. It is a straight-line sequence of phases, each raising on first failure:

1. Duplicate-service check (`lower.py:3429`).
2. Type aliases, declared-type validation, type-decl lowering, signature table,
   fn lowering, extern lowering (`lower.py:3447-3453`). Each is a `_lower_*`
   helper that raises internally. These build the type and signature tables that
   everything downstream reads.
3. The per-component loop, `for comp in program.components:` (`lower.py:3565`),
   calling `_lower_component` per component (`lower.py:3575`) and raising async
   A1 refusals inline (`lower.py:3596-3609`).
4. Whole-composition post-passes over the already-lowered table: `check_taint`
   (G9, `lower.py:3617`), spawn emission bounds (`lower.py:3629`), capability
   attenuation (`lower.py:3635`), and `_link` (`lower.py:3640`) where G2
   provision conflicts (`lower.py:6283-6288`) and G3 cycles
   (`lower.py:6360-6389`) live.

Two boundaries fall out of this structure, and they are the whole design:

- The per-component loop at `lower.py:3565`. Each iteration lowers one whole
  component body. By phase 3 the type table and signature table already exist,
  so one component's failure does not poison another's lowering. This is the
  cleanest recovery unit in the compiler.
- The whole-composition post-passes (`_link`, taint, spawn bounds) each walk
  every component and raise on the first offending entry. `_link`'s loop
  (`lower.py:6303`) is a pure pass over resolved provides/requires; it can
  collect every conflict instead of raising the first.

## The recovery model: a diagnostics sink threaded through the spine

### Where to accumulate

Recommended: a `Diagnostics` sink object threaded through `check_and_lower` and
the `_lower_*` helpers, not a rewrite that removes exceptions.

The sink is a small container: an ordered list of `RevlError` values plus a
`fatal` flag. Two operations:

- `sink.take(error)` records a refusal and returns a poison sentinel (below).
  Used where recovery is possible.
- `sink.fatal(error)` records a refusal that stops the current recovery unit,
  by raising a private `_Abort` carrying nothing (the error is already in the
  sink). Used where continuing would fabricate cascades.

Keep `RevlError` exactly as is. It stays the payload type; the sink holds
`RevlError` values, and `report` (below) maps `classify` over the list. This is
the smallest possible change to the error model: no new fields, no column
tracking required (though see the honest-notes section), and every existing
`raise RevlError(...)` becomes either `sink.take(...)` or `sink.fatal(...)`
mechanically.

Do not thread the sink as a global or a module singleton. The compiler is
re-entrant (the plan double-compile in `src/revl/plan.py` compiles twice, and
`spawn` templates lower nested). Pass the sink explicitly, one per top-level
`check_and_lower` invocation.

### How to recover: synchronization at the component boundary

The classic multi-error hazard is a bad binding poisoning every downstream use,
so one real error becomes fifty fabricated ones. The defense is
synchronization points: fixed structural boundaries at which the compiler
discards the poisoned local state and resumes clean.

The synchronization points, in order of safety:

1. Between top-level declarations (phase 2 helpers and the component loop). A
   failure lowering component B does not change the fact that component A
   lowered cleanly and component C has not been looked at yet. Wrap each
   iteration of the `lower.py:3565` loop in a `try/_Abort` boundary: catch the
   per-component abort, record it, continue to the next component. This alone
   converts "one component refuses" from a whole-compile abort into "this
   component refuses, keep checking the rest."
2. Between statements inside a component body. A failed statement's poison must
   not leak into the next statement's inference. This is finer-grained recovery
   and is optional for the first slice (see staging).
3. Between the whole-composition post-passes. `check_taint`, spawn bounds,
   attenuation, and `_link` are independent walks. Run each even if an earlier
   one recorded refusals, as long as its inputs are structurally intact (the
   component table exists). Collect from all of them.

### The poison sentinel: how to not cascade

Synchronization handles declaration- and statement-level poison. Expression-level
poison needs a value: when inference fails inside an expression,
`sink.take(...)` returns a `Poison` sentinel type that is compatible with
everything and infers to nothing. Downstream checks that receive `Poison` on
either side of a comparison, an argument position, or a binding suppress their
own refusal (they would be checking against a value that is already known bad).
This is the standard "error type" trick: one real T1 mismatch does not spawn a
mismatch at every later use of the poisoned binding, because those later uses see
`Poison` and stay silent.

Rule for `Poison`: it is absorbing (any operation involving `Poison` yields
`Poison`) and silent (no operation involving `Poison` raises). A diagnostic is
emitted only at the point poison is created, never where it propagates. This is
what prevents the fifty-fabricated-errors failure mode.

### Deduplication

Dedup is on `(code, filename, line, message)` before reporting. Two paths
reaching the same refusal (common once recovery keeps walking) collapse to one
diagnostic. Note (Change 3): this key INCLUDES filename, and does NOT match the
existing `plan._add` key (`src/revl/plan.py`), which deliberately EXCLUDES
filename because the same rejection reaches the planner under two filenames (the
gate abspaths vs the standalone compile). The two keys are intentionally
different.

## Which refusals are independently detectable vs genuinely fatal

### Independently detectable: safe to `sink.take` and continue

These are self-contained walks whose result does not depend on the code that
refused:

- Duplicate service / duplicate component (`lower.py:3429`, `lower.py:3567`) and
  duplicate/import refusals in the compiler pre-pass (`src/revl/compiler.py`
  import-privacy and cycle checks). Independent, pre-lowering.
- G1 undeclared-requirement / undeclared-name at a component boundary
  (`lower.py:3979`, `5049`). Local to one component.
- G2 provision conflicts and G3 dependency cycles in `_link`
  (`lower.py:6283`, `6360-6389`). Computed over the already-lowered component
  table; a pure post-pass. Naturally collectible across all entries.
- The async A1 reach check in the component loop (`lower.py:3596`). Per
  component; a refusal in one does not affect another.
- Typed holes (T3): already fully accumulated (`holes.py`).

### Genuinely fatal: `sink.fatal`, stop this recovery unit

These poison a shared structure that later phases read, so continuing fabricates
cascades:

- Type-alias resolution and declared-type validation failure
  (`lower.py:3447-3448`). The type table feeds every downstream signature; a
  broken type poisons all consumers. A failure here is fatal to the whole
  compile, not just one unit. Report what accumulated up to this point, then
  stop.
- Signature-table failure (`lower.py:3450-3452`). Call-site checking depends on
  it. Same treatment: fatal to the compile.
- T1/T2 type-mismatch inside an expression (`typecheck.py`, raised during
  `infer_ast`/`check_ast`). The inferred type is needed by the enclosing
  expression. Recovery here is the `Poison` sentinel plus statement-boundary
  synchronization, not full continuation; the current statement is abandoned,
  the next statement resumes.

The dividing line: a refusal is recoverable if the thing it refused is a leaf
(one component, one entry, one expression) whose output nothing structural
consumes, and fatal if it is a table (types, signatures) that the rest of the
pass reads. Phase 2 builds tables and is mostly fatal-on-failure; phase 3 (the
component loop) and phase 4 (the post-passes) are leaf walks and are recoverable.

## Scope: all-per-file is the floor, all-per-compile is reachable for lower

The stated minimum is all-per-file. The question is whether all-per-compile is
feasible or whether cross-file typing forces per-file.

Finding: revl composition is not cross-file-typed in the poisoning sense. A
multi-file compile merges declarations from several sources into one `Program`
(`lower.py:3572-3574` notes it, and roadmap 312 fixed per-source diagnostic
filenames: each component lowers under its own `comp.source`,
`lower.py:3575-3577`). Types and signatures are global to the merged program and
built once in phase 2. So the component loop already spans all files, and
recovering across it recovers across files. The whole-composition post-passes
(`_link`, taint) are inherently cross-file already.

Therefore:

- Lower-pass refusals (G1-G9, A1-A9): all-per-compile is feasible, because the
  recovery boundary (the component loop and the post-passes) already spans every
  file. Each diagnostic already carries its own `filename` via `comp.source`
  (`test_multifile_diagnostic_filename.py` guards this), so multi-file output is
  correctly attributed with no extra work.
- Phase-2 table failures (types, signatures): fatal, so a broken type in file A
  stops before file B's components are checked. This is the one place per-compile
  degrades to "everything up to the first fatal." Acceptable: a broken type
  table means downstream checking is meaningless anyway.

Recommendation: target all-per-compile for the recoverable refusals (which is
where H38's 62 sites lived: they were per-site G/A refusals, not table
failures), and accept that a fatal table error truncates the report. Document
the truncation in the output (a `truncated: true` flag) so the author knows more
may follow once the fatal error is fixed.

Parser refusals are a separate, harder question (below) and are scoped out of the
first implementation.

## Output shape

Extend the existing surfaces; do not invent a new one.

`report` grows from a one-element list to the carrier's full list (Change 3 —
no `truncated` flag; truncation is simply the phase-2 raise aborting early):

```
def report(error) -> dict:
    errors = getattr(error, "errors", None) or [error]
    return {"ok": False, "diagnostics": [classify(e) for e in errors]}
```

Each diagnostic is the existing `classify(error)` record: location
(`filename`, `line`), `message`, `code`, `category`, and the fix redirect from
the `FIXES` table. That fix line is the exclusion-diagnostic redirect the author
acts on. Nothing about the per-diagnostic shape changes; there are just N of
them, sorted by COMPILE ORDER — `(file position in the compile-args order,
line)` — so `diagnostics[0]` is stable (Change 4).

Consumers:

- CLI text mode: replace the single `error: {error}` line with a loop over the
  diagnostics, each rendered as `filename:line: message` plus its hint,
  terminated by a census line (`N refusals across M files`), mirroring the holes
  renderer at `__main__.py:618-624`.
- CLI `--json` mode: already calls `report(...)`; it now receives the full list
  for free. Exit code stays 1 on any refusal. The harness reads this JSON and
  gets every site in one parse, which is exactly the pre-scan H38 hand-rolled,
  now produced by the compiler itself.
- LSP: `compute_diagnostics` already returns a list and already documents the
  one-diagnostic limitation (`analysis.py:41-53`). Point it at the sink and the
  editor shows every squiggle at once, no client change.

Exit code contract: 0 clean, 1 on any refusal (unchanged). The count is in the
payload, not the exit code, so existing callers that only check the code keep
working.

## Staged implementation plan

Each stage is independently landable and independently valuable. A follow-up
agent should not attempt all of it in one change; the component-boundary slice
alone captures most of the H38 win.

### Stage 1: component-boundary recovery, as built (the 80%)

Implemented per the Fable corrections above — NO sink threading, NO `Poison`
(both deferred to Stage 2):

- A local `errors: list[RevlError]` in `check_and_lower`, plus a `_collect`
  helper that runs a post-pass and appends any `RevlError` instead of aborting
  (dropping a non-`RevlError` crash only while already failing, Change 4).
- Wrap each component-loop iteration in `try/except RevlError` — including the
  async A1 reach raise: on refusal, append the error AND a `_component_header_stub`
  (Change 1), then continue. A duplicate-component refusal is collected but adds
  NO stub (its name is already in the topology).
- Run `check_taint`, handoff, spawn bounds, attenuation, `_lower_fault_tests`,
  `_link`, and `_lower_tests`/`_lower_prop_tests` through `_collect`. The
  body-walking passes receive `live_components` (poisoned stubs filtered out);
  `_link` receives the FULL list plus the shared `errors` sink and collects G2
  conflicts, multi-realm route gaps, and G3 cycles (one per SCC).
- At the end of `check_and_lower`, if `errors` is non-empty, `_raise_collected`
  dedups on `(code, filename, line, message)`, sorts by compile order, and
  raises a `RevlErrors` carrier — BEFORE the IR result is assembled, since the
  result reads possibly-partial `manifest`/`components`.
- Phase-2 table failures (types/signatures, before the loop, before `errors`
  even exists) keep RAISING: the compile aborts there = the truncation behavior.

Exit tests for stage 1 (all in `tests/test_multi_error_reporting.py`):

- Headline: three components, each with one distinct G/A refusal, compiled once,
  produce three diagnostics with three distinct locations.
- A multi-file compile where files A and B each contain a refusing component
  produces both, each attributed to its own `comp.source` filename; and the
  FIRST diagnostic is the first file's refusal (compile-order stability).
- A phase-2 type-table error truncates: the report contains that one error and
  NONE of the downstream component refusals it aborts before (no cascade).
- `--json` output is a list of N well-formed `classify` records; existing
  single-error tests still pass because a single refusal still yields a
  one-element list.
- Exit code is 1 for one refusal and 1 for many (unchanged contract).
- A clean compile still returns 0, with no `poisoned` residue, and the holes
  path is untouched.

Fable exit tests (the soundness + robustness set):

- Header-stub: a body-refused component whose HEADER provides a key another
  provides — the body refusal is reported, a real G2 on that key is STILL
  reported, and no route "no provider" is fabricated.
- A partial spawner that registers a spawn site then refuses leaves `spawn_reg`
  entries but causes no crash and no fabricated spawn/attenuation diagnostic.
- `plan()` on a multi-refusal candidate returns ALL diagnostics.
- A post-pass throwing `TypeError` on poisoned state does not mask the collected
  diagnostics (and DOES propagate on an otherwise-clean compile).

### Stage 2: statement-boundary recovery and the poison sentinel

- Introduce the `Poison` sentinel in `typecheck.py`: absorbing and silent.
  `infer_ast`/`check_ast` on a mismatch call `sink.take` and return `Poison`
  instead of raising.
- Add a statement-boundary synchronization point in the component body lowering
  so a poisoned statement does not leak into the next.
- Now a single component with several independent bad statements reports all of
  them in one pass.

Exit tests for stage 2:

- A component with three independent expression-level type mismatches reports
  three, not one, and not three-plus-cascades.
- A binding whose initializer is a type error, used in five later statements,
  produces exactly one diagnostic (at the initializer), not six. This is the
  cascade-suppression regression test.

### Stage 3 (optional, separate design): parser error recovery

- Parse errors currently abort on the first raise; the parser is single-pass
  recursive descent with a top-level declaration loop (`parser.py:966`) but no
  `synchronize()`. Adding recovery means a synchronization strategy (skip to the
  next top-level declaration keyword) and an error-token concept, which is a
  larger and riskier change with its own cascade hazards.
- Recommendation: scope this out of item 386. Ship stages 1 and 2 (the lower/type
  refusals, which is where the H38 cost was) and file parser recovery as its own
  roadmap item. A file that does not parse is a different, rarer failure mode than
  a file that parses but violates six guarantees.

## Honest notes on which passes are hard to recover in

- Phase-2 tables (types, signatures) are genuinely fatal. There is no honest
  recovery from a broken type table; anything downstream is checking against
  garbage. The design accepts truncation here and flags it. Do not try to
  fabricate a placeholder type table to keep going; that manufactures exactly the
  cascade this design exists to prevent.
- The parser is the hardest and is deliberately deferred. Recursive-descent error
  recovery is a well-known source of cascade noise, and revl's parser has no
  synchronization scaffolding today. Stage 3 is a real project, not a follow-on
  tweak.
- Location granularity: `RevlError` has line but no column (`errors.py`,
  `analysis.py:76-77`). Multi-error reporting works fine with line-only
  locations, but two refusals on the same line dedup or collide under the
  `(code, filename, line, message)` key only if their messages differ. This is
  acceptable for stage 1. If same-line distinct refusals become common, adding an
  optional column is a separate small enhancement, not a blocker.
- Re-entrancy: the plan double-compile (`src/revl/plan.py`) and nested `spawn`
  template lowering both call the spine. The sink must be per-invocation and
  passed explicitly, never a module global, or the two compiles cross-contaminate.

## Summary for the implementation agent

- Recovery model: a `Diagnostics` sink threaded through `check_and_lower`, with
  `take` (recoverable, returns `Poison`) and `fatal` (truncating) operations.
  Keep `RevlError` as the payload; do not rewrite the error model.
- Cascade avoidance: synchronization at the component boundary
  (`lower.py:3565`) for stage 1, plus an absorbing-and-silent `Poison` sentinel
  with statement-boundary synchronization for stage 2. A diagnostic is emitted
  where poison is born, never where it propagates. Dedup on the existing
  `plan._add` key.
- Scope: all-per-compile for the recoverable G/A refusals (the component loop and
  post-passes already span every file, and roadmap 312 already attributes each to
  its own source file); fatal table errors truncate with a flag. Parser recovery
  deferred to its own item.
- Output: extend `report` (`diagnostics.py:204`) to map `classify` over the
  sink's list plus a `truncated` flag; the CLI text and `--json` paths and the
  LSP `compute_diagnostics` list all already consume this shape. Exit code
  unchanged (1 on any refusal).
- Land in stages: stage 1 (component-boundary recovery) captures most of the H38
  win and is a self-contained change; stage 2 (statement/expression poison) is
  additive; stage 3 (parser) is a separate roadmap item.
