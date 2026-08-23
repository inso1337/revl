# Findings — agent/uxprobe2 (probe round 2)

Probe: background job-processing architecture (`examples/uxprobe2_jobs.rvl`,
plus `examples/uxprobe2_draft.rvl` for holes and `examples/uxprobe2_fault.rvl`
for the fault path), docs-only, exercising surfaces round 1 never touched:
extern pure/acquire/@py bodies, explicit generics `[T]`, function types across
a service boundary, `checked_mod`/`checked_div_trunc` over a computed divisor,
holes, and a fault-path lifecycle test.

## 1. Refusal log

### R1 - `emission` modifier on a provide-method
Snippet: `provide queue { emission fn submit(id, payload) {...} }`
Diagnostic (verbatim): ``error: examples/uxprobe2_jobs.rvl:55: expected fn, found 'emission'``
Verdict: **friction** (borderline caught-bug). The refusal is correct -
emission-ness is inherited from the service declaration - but the message is a
bare parse error with no hint. One extra line ("provider methods are plain
`fn`; the service declaration carries `emission`") would have saved the cycle.

### R2 - `for` loop in a provide-method body
Diagnostic: ``error: ...:67: expected a statement (`let`, `effect`, `emit`,
`fail`, `if`, `return`), found 'for'`` / "revl bodies contain only effect
forms - plain expressions have no effect to record (G6)"
Verdict: **caught-bug** - I wrote stratum-1 code in stratum 3; the checker
named the exact allowed statement forms. Fix: move the loop into a pure `fn`,
delegate from the provide method (also the only way to make a function-typed
parameter useful - see gap note below).

### R3 - acquire extern without `undo`
Diagnostic: "error: examples/uxprobe2_jobs.rvl:14: acquire extern `open_ledger`
must declare `undo` (G4)" / "an `acquire` crosses into an observable effect and
needs a teardown inverse"
Verdict: **friction + [docs-gap]**. The refusal is right; the fix is
undocumented everywhere I was allowed to read (guide Host blocks says only
"`acquire` (needs `undo`) Five probe variants failed with unhelpful top-level
parse errors: `undo = @py {...}`, `undo @py {...}`, `undo { ... }`,
`undo fn() = @py {...}`, `= undo @py {...}`. Discovery came from probing
between return type and body - the actual syntax:

```revl
extern acquire fn open_ledger(path: Str) -> Int undo close_ledger(1)
  = @py { return abs(hash(path)) % 100000 }
```

Neither guide-ai-agents.md nor the `revl_grammar` MCP tool shows extern-level
`undo`/`compensate`. Longest stall of the probe.

### R4 - duplicate extern name (my typo)
Diagnostic: "error: examples/uxprobe2_jobs.rvl:20: duplicate extern `close_ledger`"
Verdict: **caught-bug** (mine), instantly diagnosable.

### R5 - lifecycle assertion failure with no detail
Behavior: "FAIL a submitted job survives until unload...: assertion failed" -
no line number, no expected/actual. The assertion was MY bug: I inserted
("job-1" -> "payload") and asserted status("job-1") == Some("job-1").
Verdict: **caught-bug** for the runtime (it held my spec against reality),
**friction** for the reporting: 3 file-surgery bisect runs (`== None` variant,
emit-removed variant, load-only variant) to find which assert and why.

### R6 - `_` is not a hole
Attempt: `let cap: Int = _`
Diagnostic: "error: ...:2: `_` is not declared in this function" / "declare it
with `let`/`var` or add it as a parameter (G1)"
Verdict: **gap** (documentation, maybe language). The probe brief assumed a
"`_` hole in a typed position"; no allowed doc defines `_` outside `match`
arms, and the checker treats it as an undeclared identifier. If `_`-as-hole is
intended it is unspecified in every agent-facing surface; if not, the brief's
premise is wrong. `hole "msg"` in an annotated let works exactly as
rejections.md T3 promises - type from context, obligation listed, draft still
compiles.

### R7 - fault-test grammar, three-step discovery
Diagnostics, verbatim, in order:
- "expected `for <component>` after the fault test name, found '{'"
- "expected `fail at ...` or `assert ...` in a fault test, found 'call'" (and again for 'load')
- "expected `step <n>` or `effect <name>` after `fail at`, found 2"
Plus a real G1 once the shape was right: "`m` is not a declared requirement of
Fragile" (from `effect Map.new() undo m.drop()` - binding-less host receiver).
Verdict: each message is **excellent** - they teach the grammar piece by piece.
The meta-verdict is **[docs-gap]**: docs/fault-tests.md holds the authoritative
syntax and was out of bounds; README's one-liner omits `for <Component>`.
Final working form:

```revl
fault test "mid-activation failure reverts its acquisition" for Fragile {
  fail at step 2
  assert no residue
}
```

## 2. Friction log

- [blocker] Extern-level `undo <expr>` syntax (acquire classification) appears
  in NO agent-facing doc and not even in `revl_grammar`. Six compile attempts.
  One example in guide-ai-agents.md Host blocks closes this permanently.
- [slow] Both probe-mandated features have their specs in excluded docs:
  holes -> docs/holes.md, fault tests -> docs/fault-tests.md. rejections.md T3
  and README's table row carried me most of the way; the rest was paid in
  compile cycles.
- [slow] Lifecycle test failures report "assertion failed" with no location
  and no values. Cost: 3 bisect runs for one wrong expected value.
- [nit] cordis-py (needed for any `lifecycle test`) is absent from the main
  venv; the pointer to backends/python/setup.sh sits mid-guide in an MCP aside.
  Setup itself worked first try, reusing an existing clone via CORDIS_PY.
- [nit] `revl_grammar` omits extern undo/compensate and fault tests - exactly
  the constructs weakest in prose docs.
- [nit] Provide-methods inherit types, so a provider cannot restate them -
  good design, but combined with R1's bare parse error first contact costs a
  cycle needlessly.

## 3. What revl gave you

- **The arithmetic contract did real work.** shard_of(job_id, workers) returns
  Result[Int, Str] over job_id.checked_mod(workers); tests pin Ok(1), the
  computed-zero path (workers == x * 0 -> Err("revl: division by zero")), and
  the div_trunc/mod pairing law (10.div_trunc(3) == 3, 10 % 3 == 1).
- **Generics without ceremony.** fn unwrap_or[T](opt: Opt[T], fallback: T)
  called at Opt[Int]/Opt[Str] in one file, explicit [T] stating intent.
- **Function types crossed a real service boundary.** service Aggregator
  declares fn fold(seed: Int, batch: List[Int], step: (Int, Int) -> Int); the
  provider receives the function value and delegates to a pure higher-order
  fold_loop; in stratum 1 retry_once(step: (Int) -> Result[Int, Str], x)
  calls through arrows with arity/type checks. Caveat: stratum 3 bodies type
  no call through an arrow (documented frontier), so the provider MUST
  delegate to a pure helper - the boundary-check design makes that natural,
  but it is a real expressiveness edge worth knowing before you design the
  service around it.
- **The runtime caught a genuine bug in my spec** (R5): wrong expected value
  in a lifecycle assert. The system testing me back.
- **The fault test is the paradigm's guarantee as a one-liner**: component
  dies at step 2, step 1's acquisition reverts, FAILED fiber contained,
  residue asserted - with a note explaining why the FAILED-fiber registry
  entry is A8 bookkeeping, not residue. Hand-rolling that is a day of plumbing.
- **Holes work as advertised**: draft compiles, stderr lists
  'expects `Int` - "worker pool size"' with file:line; filling recompiles
  clean.
- **Two findings AGAINST revl** (highest value):
  1. The extern-level `undo <expr>` slot compiles completely unchecked:
     undo close_ledger(p) (Str into Int param), undo ghost_fn(1) (undeclared
     fn), even undo close_ledger(g) (bare self-reference) all compile. The
     component-site effect/undo pair IS checked; the extern-declaration slot
     is parsed and dropped. Soundness-relevant.
  2. Fault-path `assert no residue` is weaker than lifecycle
     `assert no_residue`: a Map stub acquired with a non-inverse undo
     (`undo scratch.insert("leak", "1")`) PASSES a fault test, while the
     identical component under a plain lifecycle test fails with "residue -
     host resources never released: map#1 (new() with no drop()) (R1)".
     Either the fault path skips host-resource accounting or its revert does
     not feed the detector; both readings are bad for trusting fault tests as
     leak coverage.

## 4. Time-to-green

Compile->refuse->fix cycles: **7** (R1-R7), of which 3 were my own bugs the
tooling was right about (R2, R4, R5) and 2 were pure syntax discovery against
undocumented surfaces (R3, R7). Round 1 took 3 debug cycles inside the
well-documented core; round 2 deliberately left that core, and the count
doubled roughly in proportion to how far the mandated features sit from the
agent-facing docs.

Longest stall: extern `undo` syntax (6 attempts, R3). Runner-up: lifecycle
assertion bisection (3 runs, R5). Time-to-green: core architecture ~35 min;
full mandate including fault path and holes ~55 min.

## Verdict: language or docs?

**Docs, again, and more narrowly than round 1.** Every refusal except R6/R7's
meta-problem was me being wrong and revl being right; the diagnostics guarding
the paradigm (G4/G6/G1) remain the best error messages I get from any language.
What hurt was distribution, not capability: both features this probe existed to
exercise are specced outside the agent-facing set, and the one genuinely new
syntax form I needed (extern `undo`) is in neither the guide nor revl_grammar.
The two real defects found - the unchecked extern undo slot and the weaker
fault-path residue check - are language/runtime issues no documentation would
have surfaced, which is the probe doing its job.

## Addendum (agent/extern-undo-check) — the unchecked extern undo slot, closed

The "one genuinely new syntax form" this probe discovered turned out to be
worse than undocumented: it was parsed and DROPPED. `_lower_extern_expr`
lowered the undo/compensate expression through a lax scope with no name
resolution and no type check, so all three of the probe's hazard variants
compiled clean:

- `undo close_ledger("wrong-type")` — Str into an Int handle;
- `undo ghost_fn(1)` — callee declared nowhere;
- `undo open_ledger(path)` — the acquire calling itself at teardown.

Fixed in `_lower_externs` (src/revl/lower.py): the slot now runs through a
dedicated walk plus `check_ast`, mirroring component-site rigor. Scope
decision, justified: component-site undo sees exactly the names BOUND at
the effect site; an extern binds none, so the slot runs with an empty
variable namespace. The extern's own parameters are NOT implicitly visible
— no tier defines teardown parameter capture, and inventing that here would
be unsound speculation. Concretely enforced: declared callee only; explicit
self-reference refusal (teardown must invert, not re-acquire); arity and
argument types checked against the shared signature table (T1-style
messages).

Honest fallout — three MORE hole exploits surfaced when the check landed,
all fixed rather than exempted:

1. `tests/test_distribute.py` fixture: `undo close_sock(sock)` — `sock` is
   neither a param nor any declaration, and `close_sock` itself was never
   declared. Pure parse-and-drop exploitation. Fixture now declares
   `close_sock` and calls it with a constant.
2. **docs/syntax-2.0.md §6** — the spec's own Host-blocks example taught
   `undo close(socket)` and `compensate log_unsent(sock, data)` with neither
   name declared anywhere. The canonical documentation was exploiting the
   same hole as the probe. Rewritten to checked forms with the scoping rule
   stated inline.
3. The doc example's `close` inverse was also never declared.

close_ledger(1)-style constant-handle teardowns (the documented convention)
compile unchanged; fault/replay/lifecycle suites unaffected.
Corpus: g4_extern_undo_wrong_arg_type / _undeclared_fn / _self_call /
_param_not_in_scope, registered in REJECTIONS asserting the hint text.

Revised on review (docs/v2.0-roadmap.md item 67, 2026-08-23): the
empty-namespace rule above was the stale half of syntax-2.0 §6 — it refused
the landed WIT resource model, where the generated
`extern acquire fn r_new(...) -> R undo r_drop(...)` must name the acquired
handle. Final rule: an acquire's `undo` sees exactly ONE implicit binding,
`result: T` (the acquired value, when the extern declares a return type);
parameters stay invisible, and `compensate` still binds nothing. The WIT
importer now emits `undo r_drop(result)` with `r_drop` declared alongside,
and the corpus gained g4_extern_compensate_result (a `result` reference in
a compensate slot, refused).
