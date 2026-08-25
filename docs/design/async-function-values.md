# Design: async function values (roadmap item 92)

**Status:** design only — nothing here is implemented.
**Base:** `origin/main` @ `84df616` — every `file:line` anchor below was
verified against that sha (the frontend and emitter files are byte-identical
to `affcca9`, where the survey was made; the delta is items 46/50 only).
**Problem (harness finding #21, dogfood/findings-harness-async90.md §1;
roadmap docs/v2.0-roadmap.md:2856-2882):** items 80 + 90 gave async externs
transitive coloring for *direct* module-fn calls, but the harness's agent
loop passes the async call through a callback arrow of **sync function
type**: `agent_loop(msgs, complete: (List[Msg]) -> Str, ...)` with
`msgs => emit model.complete(msgs)` at the call site. The compile is green;
at runtime the emitted loop stays sync (`resp = complete(current)` — no
await, py and ts alike) and the arrow returns a coroutine/Promise that
leaks into `json_parse` (`the JSON object must be str…, not coroutine`).
Item 90's claim "the harness's `agent_loop` works unchanged" is falsified
by execution.

Item 90 explicitly refused first-class async values: "assuming-async would
require the emitter to `await` calls in contexts that may be sync, which
is not writable" (docs/design/async-extern.md §3, "First-class values are
refused, not widened"; the same fence is judgment call 4 of that document
and the deliberate no-widening note at src/revl/emission_analysis.py:144-148).
Item 92 lifts that refusal for the one case the harness needs — a callback
whose **declared function type carries the color** — without ever creating
a maybe-sync await.

Why the hole exists today, precisely:

- The arrow's async source is a *service operation* (`model.complete`,
  declared `async fn`), and the coloring machinery is name-based over
  externs and module fns only: `_calls_in`
  (src/revl/emission_analysis.py:18-47) records `fn` nodes and var-callee
  calls, never a req-target method call, so the fixpoint
  (`_async_callables`, emission_analysis.py:131-176) and the A1 method
  admission (src/revl/lower.py:3817-3880) simply never see
  `emit model.complete(...)` inside the arrow. Nothing fires; the leak is
  silent — the exact wrong-answer class the checker exists to prevent.
- Even if it were seen, the function type `(List[Msg]) -> Str`
  (docs/function-types.md; `FN_HEAD` normalisation at
  src/revl/typecheck.py:94-147) has no async variant, so there is nothing
  for `compatible`/`unify` (typecheck.py:342-437) to refuse and nothing
  for the emitters to key an `await` on.

---

## 1. Which fix direction

**Recommended: (c) as the mechanism, with (b)'s propagation riding on it —
a declared async-aware function type, checked into call-site arrows and
folded into the item-90 fixpoint.** Concretely:

- The *type* is the vehicle (the (a)/(c) axis): a function type may declare
  an async result, `(List[Msg]) -> Async[Str]`. This is what lets both
  colored emitters place every `await` **statically** — the position that
  killed pure inference in item 90 ("the emitter can't place awaits in
  maybe-sync contexts") is resolved by declaration, not by widening.
- The *policy* is declaration at the parameter (direction (c)): the loop
  writes `complete: (List[Msg]) -> Async[Str]`. One line in the harness.
- Direction (b) survives as **checking-position propagation**, not
  body-inference: the call-site arrow `msgs => emit model.complete(msgs)`
  is checked against the declared async type (the existing checking
  position, `_check_arrow`, src/revl/typecheck.py:1408-1455), which is
  what *admits* its async body and stamps the arrow's color into the IR;
  and a named fn that calls an async-typed parameter is colored by the
  item-90 fixpoint (§3 below), so `agent_loop` becomes async and its
  callers are governed by the *existing* A1 admission at lower.py:3850-3880
  with no new machinery.

Rejected alternatives, with reasons:

- **(b) alone — infer the arrow's color from its body and retype the
  callback parameter at each call site.** Two independent killers.
  (1) `agent_loop` is compiled once against its declared parameter type;
  whether its body's `complete(current)` awaits cannot depend on which
  caller passed which arrow — that is whole-program bidirectional
  inference across function boundaries, which revl deliberately does not
  do (the signature is the boundary; same principle as "the service
  declaration is the upper bound on its providers", lower.py:3814-3816).
  (2) On py an unconditional await of a maybe-sync result is a `TypeError`
  (`await "str"` raises), so an inferred maybe-color is unemittable —
  exactly the argument that made item 90 refuse the `*` trick
  (async-extern.md §3). A runtime `isawaitable` sniff would emit, but it
  trades a checked property for a dynamic one; refused.
- **(a) as a general type constructor** (`Async[T]` usable anywhere, or a
  `Promise`-style value type). This is what async-extern.md §1 rejected
  for extern returns: the color leaks into `unify`/`compatible`/`join` and
  into every call-site expression type, violating the stance that "awaits
  are tier-level and the boundary reading of async never appears in
  expression types" (docs/syntax-2.0.md:502-505 as cited there). We keep
  that rejection: §2 confines `Async` to exactly one position and proves
  no expression ever has type `Async[T]`.
- **A second `async (List[Msg]) -> Str` modifier spelling** was considered
  (it matches "`async` qualifies the `fn` it precedes", parser.py:895-906
  precedent). It needs a parser change in `type_()`
  (src/revl/parser.py:962-997), a new head through every `_split_fn_type`
  copy (typecheck.py:94-116 *and* backends/rust/emit.py:190), and its own
  well-formedness story. `(…) -> Async[Str]` parses **today** with zero
  parser change (`Async[Str]` is already a type application,
  parser.py:999-1009), is the roadmap's own sketch
  (v2.0-roadmap.md:2871), and — because `parse_type` gives it a distinct
  head — makes async-vs-sync mismatches *fall out* of the existing
  same-head recursion in `compatible` instead of needing new refusal code.
  **Judgment call 1 (human):** confirm the `Async[T]` spelling over the
  modifier spelling; the modifier is more consistent with the extern/method
  surface, the constructor is smaller and is what the roadmap sketched.

Reconciliation with async-extern.md's rejection of `-> Async[Str]`: that
rejection was for *extern* declarations, where a modifier slot exists and
where the call expression's type would have carried the color. A first-class
function value has no modifier slot — its type is the only place a color
can live — and §2's elimination rule unwraps at the call, so the color
still never reaches an expression type. The two decisions are the same
stance applied to two positions.

## 2. Type-system impact

### `Async` is a position-restricted annotation, not a first-class type

Invariant (the load-bearing sentence of this design): **no expression ever
has type `Async[T]`.** `Async[T]` may appear in exactly one position — the
return type of a function type — and it is eliminated at the only
elimination site a function value has (calling it), where the call
expression has type `T`. Enforced three ways:

- **Well-formedness.** `check_type_wellformed`
  (src/revl/typecheck.py:199-217) gains: `Async` has arity 1, and it is
  legal only as the *return of a function type* — the walk threads a
  position flag, so `let x: Async[Str]`, `List[Async[Str]]`,
  `Opt[Async[Str]]`, and `(Async[Str]) -> Int` (parameter position) are
  all refused with one message: *"`Async[T]` is not a value type — it may
  only be the return type of a function type, e.g.
  `(List[Msg]) -> Async[Str]` (docs/function-types.md)"*. It is **not**
  added to `_GENERIC_ARITY` (typecheck.py:196) as an ordinary generic.
- **v1 position fences (scope cut).** Even in the legal position, v1
  admits the async function type only in a **module `fn` parameter
  annotation** — the harness's shape. Refused with "not yet" hints in:
  `let`/`var` annotations (checking position at lower.py:1794-1802 —
  also the IR `let` step carries no type, lower.py:1813-1815, so the
  emitters could not name the local to await), record fields, ADT
  payloads, type aliases, extern signatures, and **service method
  signatures** (a service op is already spelled `async fn`; letting its
  *parameters* carry async fn types crosses G8 and adds an admission
  dimension — deferred, see §5/§7).
- **Generics guard.** In `unify` (typecheck.py:342-365), a `?T` type
  parameter refuses to bind an `Async`-headed actual (and `substitute`
  can never produce one), so `fn retry(f: (Int) -> R) -> R` cannot smuggle
  `Async[Str]` into an expression type through `R`. Honest refusal:
  *"generic combinators over async function types are not yet supported —
  declare the async return concretely"*. Filed follow-up, not widened.

### `compatible` / `unify` / `join`

`parse_type("(A) -> Async[T]")` is `(FN_HEAD, ["A", "Async[T]"])` — the
existing function-type variance branch (typecheck.py:419-429: parameters
contravariant, result covariant) recurses into the return position and
meets `Async[T]` vs the actual's return. Two new lines of policy there:

- **Sync flows into async — accepted, as a checked coercion.**
  `compatible("Async[T]", actual)` where `actual` is not `Async`-headed
  reduces to `compatible(T, actual)`. A function that never suspends is a
  degenerate async function; accepting it is the direction that can never
  drop a suspension. The *emitters* realize the coercion (§4): a no-op on
  ts (`await v` of a non-Promise is the value), a static wrapper on py
  (where `await v` of a non-awaitable raises). The frontend marks the
  coercion site the same way `T -> Opt[T]` injection already marks one
  (the injected `Some` call, lower.py:2440-2474 `_inject_opt` precedent),
  so both emitters read one truth instead of re-deriving it.
  **Judgment call 2 (human):** accept-with-coercion (recommended — it is
  what lets the harness's *mock* keep passing a plain sync arrow) vs.
  refuse for strict symmetry.
- **Async flows into sync — refused.** `compatible("T", "Async[T]")`
  falls out of the existing head-mismatch fall-through
  (typecheck.py:435-437) as `False` with no new code. This *is* the
  item-92 leak, refused at the type level.
- `join` (typecheck.py:440-448) is derived from `compatible` and needs no
  change: two fn types join only when one flows into the other, which the
  rules above already decide asymmetrically (join of async and sync
  variants is the async one).
- `unify`'s structural recursion (typecheck.py:360-365) handles
  `Async[T]` vs `Async[U]` elementwise; only the tparam-binding guard
  above is new.

### Elimination: calling an async-typed value

`call_function_value` (typecheck.py:1382-1405) types a call through a
function-typed value and currently returns `returns` verbatim. New rule:
when `returns` is `Async`-headed, the call expression's type is the
**unwrapped** `T` — the tier-level await is implicit, exactly as `call
key.op(...)` on an async operation "is awaited by the driver"
(the A1 stance quoted throughout async-extern.md §2). The *admission* that
such a call may only occur in an async context is lower's job, not the
checker's (§3) — same split as emissions (typecheck types, lower admits).

### Checking an arrow against an async function type

`_check_arrow` (typecheck.py:1408-1455) already checks the body against
`want_return`; with `want_return = Async[Str]` the body is checked against
the *unwrapped* `Str` (the color is not about the value's shape), and
`_resolve_arrow` (typecheck.py:1345-1360) writes `Async[Str]` back onto
the AST node as `expr.returns` — which lowering already copies into the
arrow IR node's `returns` key verbatim (lower.py:2393-2398 pure path,
3109-3111 component path). The color therefore reaches the IR through the
existing pipe with no new plumbing; what is new is admission (§3) and the
explicit flag (§4).

### G-guarantees and declared boundaries

Async-ness of a callback is now a **declared property of a signature**,
the same principle as emission-ness and method async-ness ("the service
declaration is the upper bound on its providers", lower.py:3814-3816; the
admission gate already treats a method async flip as breaking,
src/revl/admission.py:246-250). The emission machinery is untouched: an
emitting arrow passed as a value still widens to `*`
(emission_analysis.py:93-113, `_method_emissions`'s value branch at
emission_analysis.py:366-373) — emission and async remain parallel
analyses, never merged.

## 3. Coloring — the fixpoint extended to declared first-class values

### New async-reach sources

A body can now reach a suspension three ways; the analysis must see all
three (today it sees only the first):

1. a call of a **named** async callable — async externs and colored fns
   (the existing seed/closure, emission_analysis.py:156-176);
2. a call of a **function-typed value whose declared type returns
   `Async[…]`** — in v1, only a *parameter* of the enclosing fn (the
   param types are on the IR entry, lower.py:888-894, and on the decl);
3. an `emit`/`call` of a **req-target service operation declared
   `async fn`** (`decl.async_` on the MethodDecl, parser.py:954-958;
   IR `"async": True` on the method at lower.py:2689). This closes the
   hole that let finding #21 through: it is added to the *arrow/method
   body* analysis in lower (which has `env.services`/`env.requires`),
   **not** to `_async_callables` — module-fn bodies cannot touch req keys,
   so the module-level fixpoint needs only rule 2.

### The fixpoint rule (module fns)

`_async_callables` (emission_analysis.py:131-176) gains one rule, applied
per fn alongside the existing "calls a colored name": *a fn is colored if
its body calls one of its own parameters whose declared type is an async
function type.* This is a **seed-shaped rule computed from declarations**
— it adds no iteration dynamics; the closure stays the same monotone
least-fixed-point over a finite name set, and the termination and
acyclic-witness arguments (emission_analysis.py:149-155, 195-207) carry
unchanged. The witness for a fn colored this way is a self-evident one-hop
step ("calls its parameter `complete`, declared
`(List[Msg]) -> Async[Str]`") — a new `TraceStep` detail string, no new
witness-graph shape.

So for the harness: `agent_loop` is colored by rule 2 → stamped
`"async": True` on its IR entry (the existing stamp at lower.py:2558-2559)
→ the provide method `run` that calls `agent_loop(history, arrow)` is
governed by the **existing** sync-method A1 admission
(lower.py:3850-3880): `run` must be declared `async fn`, with the same
witness-chain diagnostic that already exists. Propagation terminates at
the service declaration exactly as before (async-extern.md §3,
"Boundaries").

### Where the arrow itself is admitted and colored

An arrow is classified at its lowering site, after its body is lowered.
The async-reach of the body is computed by a helper in the
`_async_reached_outside_provide` family (lower.py:3269-3286): calls into
`async_colored` (rules 1–2) plus req-target async ops (rule 3, component
path only — the pure path, lower.py:2380-2399, has no reqs by
construction). Then:

- **checked against an async fn type** (`expr.returns` is `Async`-headed):
  the arrow is *admitted* and stamped async in the IR (§4). Its body may
  reach async callables and async ops.
- **async body, sync or absent declared type: refused.** The new item-92
  diagnostic, and the sound replacement for today's silent leak:

  > this arrow reaches async operation `model.complete` (declared
  > `async fn` in service `Model`), but its type `(List[Msg]) -> Str`
  > carries no async color — the caller would receive an unawaited
  > suspension (A1).
  > hint: declare the receiving parameter
  > `(List[Msg]) -> Async[Str]` so every call through it is awaited, or
  > move the suspending call out of the arrow.
  > code=A1, category=async-propagation

  Fixture: `examples/rejections/a1_async_arrow_sync_type.rvl` (naming
  precedent: `examples/rejections/a1_async_extern_sync_method.rvl`).
  Ordering note: for pure-fn bodies this check must run in the post-pass
  at lower.py:2540-2559 (where `async_colored` exists — `_lower_fns` at
  2517 runs before the fixpoint at 2531), extended to walk arrow nodes;
  the component path (lowered from 2574 on) already has the colored set
  in hand.
- **sync body checked against an async type:** admitted; this is the
  coercion case of §2 (the mock's arrow).

### Attribution: the arrow owns its color, not its constructor

Today `_calls_in` recurses through arrow bodies (emission_analysis.py:41-44
walks every dict value), so an arrow-internal call is attributed to the
enclosing body — which would (a) spuriously color a fn that merely
*constructs* an async arrow without calling it, and (b) spuriously refuse
the constructing sync context. The walks gain a stop-at-async-arrows mode:
an arrow node carrying the async flag is a *value* whose suspension belongs
to whoever awaits it — its internal calls are not the constructor's.
Sync arrows keep bubbling exactly as today (conservative, and required for
emission-analysis compatibility, which keeps its own walk untouched).
This refinement applies to the fn post-pass (lower.py:2540-2559) and the
method admission (`_calls_in(mbody, …)` at lower.py:3818-3820); the
*value-position* refusals for named async callables (next paragraph) are
unaffected.

### What stays refused

- **Named async callables in value position** — the v1 fence stays:
  `function `f` uses async callable `http_post` as a function value…`
  (lower.py:2540-2557) and its method twin (lower.py:3837-3849) keep
  firing, messages byte-identical, with the hint extended by one clause:
  *"…or receive it through a parameter declared `(…) -> Async[T]` and
  wrap it in an arrow"*. Lifting the fence for bare names (so `http_post`
  itself flows where `(Str) -> Async[Str]` is expected, eta-expansion-free)
  is mechanical once §2 exists but is deliberately a follow-up slice.
  **Judgment call 6 (human):** accept the v1 asymmetry (the arrow
  `s => http_post(s)` is admitted where the bare name is refused).
- **Setup/activation bodies** — construction of an async arrow in a setup
  body is fine (it is a value), but *calling* anything colored stays
  refused (lower.py:2584-2606, `_async_reached_outside_provide` pruning
  provide bodies at 3269-3286): divert/inertia semantics remain out of
  scope, unchanged from item 80.
- **Pure tests, `undo`/`compensate` slots, `verified fn`** — unchanged
  refusals; plus one new validity line: a `verified fn` may not declare an
  async-typed parameter (totality has no suspension story).
- **Genuinely maybe-sync contexts** — there are none left by construction:
  a value either has an async-colored type (all its call sites await, the
  enclosing fn is colored) or a sync type (its body may not reach async).
  The maybe-sync case that killed inference cannot be *expressed*, which
  is the design's answer to it.

## 4. IR and per-tier emission

### IR (additive; no new sections)

- **Arrow node:** `"async": true` when the arrow was checked against an
  async function type — mirroring the extern (lower.py:1348), colored-fn
  (lower.py:2558-2559), and method (lower.py:2689) spellings, so every
  emitter reads one shape (`.get("async")`) instead of parsing the
  `returns` string. The `returns` key already carries `Async[Str]`
  verbatim through the existing pipe (§2).
- **Fn entries:** nothing new — params already carry the author's declared
  type strings (lower.py:888-894), which now may read
  `(List[Msg]) -> Async[Str]`; the colored-fn `"async": True` stamp is
  item 90's.
- **Coercion marker:** a sync value meeting an async-typed parameter is
  wrapped at the frontend in a marker call node (the `_inject_opt`
  precedent, lower.py:2440-2474) — proposed spelling: a
  `{"kind": "as_async", "value": …}` node. Emitters that erase the color
  (rust/go/java) erase the node to its value.
- **IR version: stays 3** — same reasoning and same tension as
  async-extern.md §4 (additive keys; old colored-tier emitters fail loud,
  blocking-tier emitters correctly ignore). **Judgment call 5 (human):**
  reconfirm, since this adds a *node kind* (`as_async`), which an old
  emitter refuses loudly by its unknown-kind path — still not silent.

### typescript — backends/typescript/emit.py

- **Type rendering:** `_ts_v3_type` (:1170-1203) gains an `Async` case in
  the `[`-branch: `Async[T]` → `Promise<{T}>` — today it would fall to the
  generic fallback `Async<unknown>` (:1202). A fn-type return position
  then renders `((a0: List<…>) => Promise<string>)` through the existing
  FN branch (:1179-1187).
- **Async arrow:** the arrow branch (:679-732) reads the node's async
  flag: prefix `async `, and render the body with `in_async=True` so
  internal async-extern calls are awaited by the existing machinery
  (:488-502). Equally important: a **sync** arrow must render its body
  with `in_async=False` — today the body context inherits the enclosing
  `ctx.in_async` (:708-714), a latent bug that item 92 would make live
  (an `await` inside a non-async arrow is a tsc error). The IIFE capture
  snapshot (:715-732) is orthogonal and unchanged.
- **Awaiting the callback:** `_Ctx.async_names` (:1226-1237) holds global
  names only. Add a per-body set of **async value locals** — the enclosing
  fn's parameters whose declared type is an async fn type — threaded where
  `_emit_ts_functions` builds the fn context (:1962-1990, the
  `fn.get("async")` branch at :1977-1982) and where `_provide_impl` builds
  the method context (:904-925). The v3 call branch (:482-503) awaits when
  the var-callee names either set; the existing EmitError-on-sync-context
  contract (:496-501) covers both.
- **Coercion:** the `as_async` node is a no-op wrapper (emit the value) —
  awaiting a non-Promise is the value in JS.
- Req-target calls stay un-awaited inside method bodies (:474-481): inside
  an async arrow the tail-call Promise flattens into the arrow's own
  Promise, which the (awaiting) caller settles — same reason the driver
  awaits at :2120 today.

### python — backends/python/emit.py (the largest emitter slice)

py currently has **no** fn-level async machinery at all: module fns emit
plain `def` (:1266-1277), externs erase the flag into blocking bodies
(:1280-1295 — correct on this tier, async-extern.md §8), and only provide
methods go async (:660-672). Item 92's exit test needs `agent_loop` to be
`async def` with `resp = await complete(current)`, so the py analog of
item 90's ts slice must now be built:

- **Colored fns:** `"async": True` fn entries emit `async def`; call sites
  award `await` when the callee is a **colored fn name or an async-typed
  parameter name — deliberately NOT an extern name**. On py the async
  extern erased to a blocking `def`; awaiting its (non-awaitable) result
  raises `TypeError`. The per-tier await seed differs from ts's and the
  design says so out loud: ts awaits {async externs, colored fns, async
  locals}; py awaits {colored fns, async locals}. A colored `async def`
  fn whose only async reach is an erased extern simply contains no await —
  valid Python, correct A1 semantics.
- **Async arrows:** py arrows are lambdas (:493-495, :1137-1141) and a
  lambda cannot be `async`. Three statically-decided shapes:
  1. body is a *tail call of an async callable* (the harness's
     `msgs => emit model.complete(msgs)` — an `async def` method on this
     tier): emit the plain lambda; it returns the coroutine, and the
     awaiting call site settles it. No wrapper needed.
  2. body is *statically sync* (the mock's arrow, and any sync→async
     coercion of a named value): wrap — `_revl_as_async(lambda …: body)`
     where the helper is four lines of emitted preamble
     (`def _revl_as_async(f):` / `async def g(*a): return f(*a)` /
     `return g`). Static, no `isawaitable` sniffing.
  3. body needs an *internal* await (an async call in non-tail position,
     e.g. `msgs => json_parse(complete2(msgs))`): inexpressible in a
     lambda; **refused honestly at emit** in v1 — *"an async arrow body on
     the py tier must be a single call of an async callable or fully sync
     — hoist the mixed body into a named fn (it will be async-colored)"*.
     **Judgment call 4 (human):** accept this v1 tier limit (ts has no
     such limit; the hoisted-named-fn escape is always available) vs.
     requiring statement-hoisting of a local `async def` now.
- The asyncio lifecycle driver (:1362-1441, `_revl_call` awaiting flagged
  methods :1431-1441) is unchanged.

#### The invariant behind the arrow shapes: an `await` never lands in a lambda

Shapes 1-3 all serve one rule: on py an `await` may appear only in an
`async def` frame, never in a lambda (a lambda is always a sync frame, and
`await` inside one is an import-time `SyntaxError`). Two later lighthouse
findings were the same rule violated in different emitter positions, both
admitted green then dying at py exec time (the item-78 compiles-implies-runs
class):

- **item 263 — `match` in an async body.** The match binder rides one-shot
  lambdas (`(lambda match: …)(scrut)`, `(lambda bind: …)(payload)`). An arm
  that crosses an async boundary (`Go => emit store.get(k)`) renders an
  `await`, which the lambda then traps. Fix: in an async frame `_match_expr`
  switches the binder to walrus assignments carried by `(<bind>, <body>)[1]`,
  so the arm helper hoisted out of the async body inherits the async color and
  the `await` lands at the enclosing `async def`'s top level.
- **item 264 — an async arrow re-passed from an async frame.** Following the
  wrap hint (`h => complete(h)`, `complete` an async local) inside an async
  caller hit shape 2's wrapper while the await-seed still fired in the arrow
  body, emitting `_revl_as_async(lambda h: (await complete(h)))`. The module
  `_expr` had no in-arrow suppression (the component emitter got it in item
  141) and did not thread the async-typed params into the arrow's coroutine
  predicate. Fix (emitter, not a checker refusal: the arrow is a legal shape-1
  tail call): suppress the await-seed inside an arrow body and thread the
  async locals, so the arrow renders as the plain coroutine lambda shape 1
  already prescribes.

### rust, go, java — erasure

`Async[T]` erases to `T` in each backend's type rendering (rust:
`_rust_fn_type`/`_rust_type`, backends/rust/emit.py:171-187 — the
position-aware `impl Fn` lowering from item 91 then applies unchanged; go
and java strip it wherever fn types render at all), and the `as_async`
node erases to its value. Sound for the same reason the extern flag erases
(async-extern.md §2, family 2): on these tiers the async sources are
blocking (externs) or synchronous (methods), so the awaitable the color
tracks never exists — the callback returns the plain value. One-line strip
per backend; the memory-noted rule applies: run each backend's own golden
suite, `pytest tests/` does not (revl-wave-backend-golden-gap).

### wasm — refuses

A fn declaring an async-typed parameter, or an async arrow, is refused at
emit with the async-extern shape (backends/wasm/emit.py's existing honest
refusals): the substrate has no host async seam beyond `await Job.run`.

## 5. Compatibility with items 80 and 90

- **The direct-call path is byte-identical.** The fixpoint's existing seed
  and closure rules are untouched (the new rule only *adds* colored names);
  the extern IR flag, the ts extern/colored-fn emission (:2014, :1977),
  and the A1 method admission all run as today. Every test in
  tests/test_async_extern.py (parser validity through
  `test_ts_emits_colored_fn_as_async_with_awaited_transitive_call`) must
  pass unmodified — that is the slice-exit bar for every frontend slice.
- **Refusals that stay, verbatim:** `pure`/`acquire`/`compensate` validity
  (lower.py:1310-1330); sync-method-reaches-async with its witness chain
  (lower.py:3850-3880); the setup/activation-body refusal
  (lower.py:2597-2606); named-async-callable-in-value-position
  (lower.py:2540-2557 and 3837-3849 — hint extended, message head
  unchanged); the wasm and no-@tier-body refusals.
- **One deliberate behavior change:** a sync-typed arrow whose body reaches
  async flips from *compiles-then-leaks-a-coroutine* to *compile error*
  (§3's new diagnostic). A program that compiled yesterday errors today —
  but yesterday's compile was a silent wrong answer at runtime, the
  contract-errata class, and the error names the one-line fix.
  **Judgment call 3 (human):** confirm this flip lands with no
  grace/warning period. (Recommended: yes — finding #21 is the proof the
  silent version costs more.)
- Item 90's roadmap claim is corrected by item 92's entry already; the
  landing note should mark 92 ✅ and cross-reference the finding.

## 6. Implementation plan — ordered, landable slices

Wave context: the frontend files (parser.py, lower.py, typecheck.py,
admission.py) are item-53's this wave (hot-swap handoff), placement is
item-46's, tools/mcp + bench are item-50's. **Slices 1–2 must land after
item 53**; the emitter slices touch no live-agent files. Nothing here
touches `backends/*/runtime.*` or `selfhost/**` (the selfhost checker
trails as usual — filed follow-up).

| # | Slice | Files | Depends on | Tests / exit bar |
| --- | --- | --- | --- | --- |
| 1 | **`Async` in the type algebra.** Well-formedness (arity 1, fn-return-only, v1 param-position fences), `compatible` (sync→async accept, async→sync refuse), `unify` tparam guard, `call_function_value` unwrap, `_check_arrow`/`_resolve_arrow` against `Async[…]` returns. | src/revl/typecheck.py; tests/test_async_fn_values.py (new) | item 53 landed | unit tests over compatible/unify/join/wellformed; test_function_types.py + test_async_extern.py green unchanged |
| 2 | **Frontend admission + coloring.** Arrow async flag in IR; the new sync-typed-arrow refusal (+ rejection fixture); async-reach helper incl. req-target async ops; fixpoint rule 2 (async-typed params) with witness step; stop-at-async-arrow attribution; `as_async` coercion node; verified-fn validity line. | src/revl/lower.py, src/revl/emission_analysis.py, examples/rejections/a1_async_arrow_sync_type.rvl, tests | 1 | rejection + acceptance frontend tests; the agent_loop shape typechecks and colors (`agent_loop` async, `run` must be `async fn`); all existing A1 messages byte-identical |
| 3 | **ts emitter.** `Async` type rendering (`Promise<T>`), async arrow + sync-arrow `in_async` isolation, async value locals in `_Ctx`, awaited callback calls, `as_async` no-op. Golden + tsc gate. | backends/typescript/emit.py, backends/typescript/golden/**, its test suite | 2 | vitest + `npm run typecheck` on an agent_loop-shaped golden |
| 4 | **py emitter** (largest). `async def` colored fns; call-site awaits (fns + async locals, not externs); the three arrow shapes + `_revl_as_async`; mixed-body refusal. | backends/python/emit.py, py goldens/tests | 2 (parallel with 3) | asyncio round-trip: mock arrow (coerced) and async-op arrow both settle; no `never awaited` warning |
| 5 | **Blocking tiers + wasm + docs.** rust/go/java `Async` strip + `as_async` erasure; wasm refusal; docs/function-types.md §, docs/backend-ir-v3.md arrow-flag line, async-extern.md judgment-call-4 cross-ref, roadmap 92. | backends/rust/emit.py, backends/go/emit.py, backends/java/emit.py, backends/wasm/emit.py, docs/ | 2 (parallel with 3, 4) | per-backend golden suites (not just `pytest tests/`) |
| 6 | **Exit test + dogfood.** The harness's real `agent_loop` (callback-arrow shape) with the one-line `-> Async[Str]` declaration: mock+loop lifecycle green on **py and ts**, no coroutine/Promise leak; dogfood findings entry; roadmap 92 ✅. | tests + dogfood/ + docs/v2.0-roadmap.md (pull-rebase first) | 3 + 4 | the finding-#21 repro, green |
| 7 | **Follow-ups (filed, not sliced).** Named async callables in value position; `Async` in lets/fields/aliases/service signatures + the admission dimension; generics × `Async`; py mixed-body hoisting; selfhost checker; tree-sitter (none needed for `Async[T]` — it is a plain application — verify). | — | — | — |

### Parallelization / collision map

- **Sequential spine: 53 → 1 → 2.** Slices 1 and 2 both live in item-53's
  file set; run them as one agent (or strictly ordered — 1 is
  typecheck.py-only, 2 is lower.py + emission_analysis.py, so two agents
  *can* interleave, but 2 consumes 1's API).
- **Fan-out after 2: slices 3, 4, 5 are pairwise disjoint**
  (backends/typescript/**, backends/python/**, the four other backends +
  docs) — three agents in parallel, no collisions with each other or with
  items 46/50.
- **Slice 6 last** (needs 3 + 4); roadmap edits pull-rebase-first
  (orchestrator owns the roadmap; 🚧 on spawn, ✅ on land).
- **Golden discipline:** every emitter slice runs its backend's own suite;
  `pytest tests/` alone is a known false green.

### Test strategy (exit test first)

The exit test is finding #21 made a fixture: module fn
`agent_loop(current: List[Msg], complete: (List[Msg]) -> Async[Str], …) -> Str`
whose body binds `let resp = complete(current)` and feeds `resp` onward;
service `Model { async fn complete(msgs: List[Msg]) -> Str }`; a provide
method `async fn run` calling
`agent_loop(history, msgs => emit model.complete(msgs), …)` (the FR-1
arrow shape the frontend already binds, lower.py:3086-3092); a mock
lifecycle test passing a **plain sync arrow** (exercising the coercion).
Green means: py asyncio run parses real strings (no
`not coroutine`, no `never awaited` warning), and ts passes
`tsc --noEmit` + vitest with the loop emitted as
`async function agent_loop` awaiting `complete(current)`. Around it:
rejection tests for every §3 refusal, the byte-identity bar on existing A1
messages, per-backend goldens, and a rust/go compile check that the erased
form still lowers through item 91's `impl Fn` path.

## 7. Smallest first slice vs. the full feature

**Smallest cut that greens the harness: slices 1 + 2 + 3 + 4, with the v1
fences as specified** — `Async[T]` only in module-fn parameter positions;
arrows colored only in checking position; named async callables still
refused as values; py arrows tail-call-or-sync only; no service-signature
or let positions; no generics interplay. The harness pays one declaration
line (`complete: (List[Msg]) -> Async[Str]`); every call-site arrow and the
mock are unchanged. Slice 5 (erasure/wasm/docs) should land in the same
wave for six-tier honesty but the harness is green without it (rust/go
already refuse via missing @tier bodies on the harness externs).

An even smaller **slice 0 option** exists — land only §3's refusal (turn
the silent coroutine leak into a compile error) without the `Async` type —
but its hint would name a type that does not exist yet; recommended only
if the wave needs to ship safety before the feature. Otherwise the refusal
lands inside slice 2, where its hint is immediately actionable.

The full first-class-async-value feature = the smallest cut + slice 5 +
the slice-7 follow-ups (value-position names, wider positions + admission
dimension, generics, py hoisting). Everything conceptually new is in
slices 1–2; the emitter slices are plumbing for a flag and a type the
frontend has already proven.

---

## Judgment calls a human should confirm before implementation

1. **Spelling:** `(…) -> Async[Str]` (recommended: zero parser change,
   roadmap's sketch, refusals fall out of head recursion) vs.
   `async (…) -> Str` modifier (more consistent with the extern/method
   surface, costs a parser + every-`_split_fn_type` change).
2. **Sync→async assignability** with a checked coercion (recommended;
   keeps the harness mock a plain arrow) vs. strict refusal.
3. **The compile-error flip** for previously-compiling leaky programs
   (finding #21's shape) with no warning period.
4. **py v1 arrow limit:** mixed (non-tail-async) arrow bodies refused at
   emit with a hoist hint, vs. building statement-hoisting now.
5. **IR version stays 3** despite an additive arrow key and one new
   `as_async` node kind (old emitters fail loud, never silent).
6. **v1 asymmetry:** the arrow `s => http_post(s)` is admitted where the
   bare name `http_post` in value position stays refused (follow-up lifts
   it).
7. **"One declared line" is the contract:** the harness edits its
   `complete` parameter type; zero-change (`agent_loop` untouched) would
   require caller-driven inference, which §1 rejects. Item 90's "works
   unchanged" ambition is retired, not re-promised.
