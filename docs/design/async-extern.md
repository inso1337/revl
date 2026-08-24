# Design: `async` externs (roadmap item 80)

**Status:** design only — nothing here is implemented.
**Base:** `origin/main` @ `9958d75` — every `file:line` anchor below was
verified against that sha.
**Problem (harness milestone 2, finding #15; docs/v2.0-roadmap.md:2466):**
the harness's single G8 crossing, `http_post`, has a `@ts` body
`return fetch(url, ...).then(r => r.text())` — a `Promise<string>` — but the
extern declares `-> Str` and the ts emitter types it `string`
(backends/typescript/emit.py:1894, `returns = _ts_v3_type(...)`), so `tsc`
rejects `Type 'Promise<string>' is not assignable to type 'string'`. Extern
bodies cannot be async today; a real HTTP/LLM call on the ts tier has no
clean spelling. JS has no blocking fetch, so "write it sync" is not an
option on that tier.

The prior investigation's conclusions all verified:

- The fix cannot live in the ts emitter alone: making a caller `await` turns
  that caller into an `async` function returning `Promise<…>`, which colors
  *its* callers — a whole-program property that belongs in the frontend
  checker (lower.py's checking passes), not in an emitter.
- There is no async signal in the IR. The extern IR node is built in
  src/revl/lower.py:1265 (`_lower_externs`), entry dict at lower.py:1296-1303
  — `name/class/params/returns/bodies` (+ optional `undo`/`compensate`),
  nothing about asynchrony.
- revl already has `async fn` service operations (docs/syntax-2.0.md:344-355,
  services 2.0 §5; amendment A1 allows `await` of host async values in async
  provide methods). The ts emitter already lowers them:
  `method_is_async` plumbed at backends/typescript/emit.py:757-758 and
  :853-857, the `await` step at :807-815, `Promise<T>` at the service
  interface at :2135-2136 and :2220-2221. The python emitter mirrors it
  (backends/python/emit.py:660-672, :726-729). This design extends that
  machinery; it does not invent a second async system.
- The reference tier (python) has no async-*extern* concept, so this
  document defines what an async extern means on each tier, anchored in the
  A1 semantics that already exist.

---

## 1. Surface syntax

**Chosen spelling: `async` as an extern modifier between the classification
and `fn`.**

```revl sketch
extern emission async fn http_post(url: Str, body: Str) -> Str
  = @ts {
      const r = await fetch(url, { method: "POST", body })
      return await r.text()
    }
  = @py {
      import urllib.request
      return urllib.request.urlopen(url, body.encode()).read().decode()
    }
```

Why this shape:

- It mirrors the one async spelling revl already has. In a service
  declaration, `async` is a *modifier* parsed among
  `emission | async | commutative | idempotent` immediately before `fn`
  (src/revl/parser.py:895-906, grammar comment at parser.py:8). Putting the
  extern's `async` in the same position — after the mandatory
  classification, before `fn` — keeps one rule: "`async` qualifies the
  `fn` it precedes."
- The classification stays first and stays mandatory, so the existing
  "unclassified extern" diagnostic (parser.py:808-815) is untouched.

Rejected alternatives:

- **Auto-detect an async body** (look for `Promise`/`await`/`.then` in the
  `@ts` text). Host bodies are verbatim tier code the frontend deliberately
  does not parse ("Host blocks — full power, honestly labeled",
  docs/syntax-2.0.md §6); detection would be per-tier heuristics, and —
  fatally — the *coloring* of callers (§3) is a whole-program, tier-
  independent property. The checker's answer cannot depend on which tier
  you later emit. The author declares; the checker enforces.
- **A return-type marker** (`-> Async[Str]` / `-> Promise[Str]`). This puts
  a color into the type grammar, so it leaks into `unify`/`compatible`/
  `join` (src/revl/typecheck.py:342, :387, :440) and into every call-site
  type. revl's established stance is the opposite: awaits are tier-level
  and the boundary reading of async never appears in expression types — a
  lifecycle test calling an `async fn` operation "does not spell `await`,
  because the boundary reading of `await` belongs to activation bodies
  only" (docs/syntax-2.0.md:502-505). `-> Str` should keep meaning `Str`.

### Touch points

| Place | Change |
| --- | --- |
| src/revl/lexer.py:19 | none — `async` is already a keyword |
| src/revl/parser.py:254 (`ExternDecl`) | add `async_: bool = False` field (name matches `ServiceMethod.async_`, parser.py:42, and `ProvideMethod.async_`, parser.py:186) |
| src/revl/parser.py:806-853 (`extern_decl`) | after consuming the classification (parser.py:816), accept an optional `kw async` before `self.expect("kw", "fn")` at parser.py:817 |
| src/revl/formatter.py:67-75 | none — `async` is already in `_KEYWORDS` (formatter.py:72), and the formatter is token-stream based |
| tree-sitter-revl/grammar.js:168-181 (`extern_declaration`) | add `optional('async')` between the classification choice (grammar.js:172) and `'fn'` — isolated, its own slice |
| selfhost/checker.rvl:654-666 (`p_extern`) | **deliberately untouched** in this feature (live agents own selfhost); the selfhost checker will refuse `async` externs until taught, which is its normal trailing position. Filed as follow-up. |

### Validity rules (checked in lower, not the parser)

v1 restricts the modifier to the shape the harness needs, refusing the rest
with honest messages (all raised in `_lower_externs`, next to the existing
classification rules at lower.py:1272-1289):

- `async` is legal **only on `emission` externs**. Rationale:
  - `pure async` — a suspension is an observable scheduling effect, and
    pure externs are callable from every pure expression position (tests,
    match guards, undo slots), which would color contexts that have no
    async story. Refused: *"`pure` externs cannot be `async` — a suspension
    is observable; classify it `emission`"*.
  - `acquire async` — an acquire's `undo` runs on the teardown/unwind path,
    which is synchronous on every tier (the ts fiber accumulator runs undos
    as plain closures, backends/typescript/emit.py:788-796); an async
    acquire would need an awaited teardown. Refused with a hint to file the
    need if it ever becomes real.
- An async extern **cannot declare `compensate`** in v1. The ts emitter
  lowers `emit … compensate …` into a synchronous `ctx.effect(() => { … })`
  closure (backends/typescript/emit.py:798-804); an `await` cannot live
  inside that callback without changing the cordis-ts effect seam. Refused:
  *"an `async` extern cannot declare `compensate` yet — the compensation
  seam is synchronous on every tier"*.
- The `undo`/`compensate` slot of *any* extern may not call an async extern
  (same reason; enforced in `_check_extern_undo`, lower.py:1138-1262, which
  already restricts the slot to a plain call to a declared callable,
  lower.py:1157-1235).

## 2. Semantics — one meaning, six tiers

> **An `async` extern is an emission whose host operation may suspend the
> calling task until a host completion arrives. Its revl type is
> unchanged: `-> Str` means every call expression has type `Str`. The
> awaitable (`Promise`, coroutine, future) is a tier-level artifact that
> never enters revl's type system or the IR type language.**

This is exactly the A1 reading revl already ships for `async fn` service
operations: the `await` "is not a divert boundary — boundaries exist only
in activation bodies. Same keyword, and the meaning is TS's own (suspend on
a promise)" (docs/syntax-2.0.md:349-355). An async extern is the
extern-shaped end of the same seam: the async provide method that A1
permits to `await` host async values now has a *declared, typed* host async
value to await, instead of smuggling one through an untyped host object.

Uniformity across the six tiers splits them into three families, matching
what each tier's concurrency model actually is:

1. **Colored tiers — ts, py.** Suspension is a function color. The extern
   emits as an async function (`async function … : Promise<T>` /
   `async def`), and *every call site the checker admitted* is awaited by
   the emitter. The author never writes `await` in revl at a call site —
   same policy as `call key.op(...)` on async operations, which "is awaited
   by the driver" (docs/syntax-2.0.md:502-505).
2. **Blocking tiers — go, java, rust (v1).** A host call already parks the
   calling goroutine/thread; suspension is not a color. The marker
   **erases**: the extern emits exactly as today, and the `@go`/`@java`/
   `@rs` body must be written blocking (`net/http`, `HttpClient.send`,
   `reqwest::blocking`). This is observably equivalent under A1 — the
   caller resumes with the value after completion either way; revl promises
   ordering within a task, not interleaving. Precedent: these three
   emitters already erase the *method-level* `async` flag today — no
   `get("async")` read exists in backends/rust/emit.py, backends/go/emit.py
   or backends/java/emit.py (the only consumers are ts,
   backends/typescript/emit.py:856/:1995/:2135/:2220, and py,
   backends/python/emit.py:662/:1778).
3. **wasm — refuses.** The substrate's only async host seam is
   `await Job.run` (backends/wasm/emit.py:914-924); there is no host I/O to
   suspend on. An async extern is refused at emit with the same honest
   shape as the existing `no @wasm body` refusal
   (backends/wasm/emit.py:785-793).

**Value type at the boundary:** nothing changes in revl's types. On colored
tiers the *emitted* signature is awaitable-of-T (`Promise<string>`,
`Coroutine[..., str]`); at a service interface an async operation already
shows `Promise<T>` on ts (backends/typescript/emit.py:2135-2136, 2220-2221)
and the driver awaits it. The G8 crossing surface is unchanged: an async
extern is still an `emission` extern, enumerated exactly as before in the
interchange document's `externs` reach set
(schema/revl-interchange-v1.schema.json:120-124).

## 3. Coloring propagation

### Who may call an async extern

The async color must never reach a context that cannot suspend. Contexts,
and their verdicts:

| Context | v1 (scope cut) | v2 (full) |
| --- | --- | --- |
| provide method declared `async fn` in its service | **allowed** | allowed |
| provide method declared sync | **refused** (diagnostic below) | refused |
| module `fn` body | refused ("phase 2" hint) | **colors the fn** (fixpoint) |
| component setup/activation body (`emit` step, `effect`, `await` step) | refused | refused (revisit separately — see below) |
| pure `test` block / lifecycle `assert` expression | refused | refused (hint: drive it through a lifecycle `call` of an async operation, or a mock) |
| extern `undo`/`compensate` slot | refused (§1) | refused |
| `verified fn` | refused (totality checking has no suspension story, lower.py:836 `_check_verified_totality`) | refused |
| first-class use (arrow value, fn reference) | refused | refused |

### The declared-sync-but-async diagnostic (the heart of the checker change)

revl already has exactly this check for emissions, and the async check is
its twin. At src/revl/lower.py:3593-3644, after a provide-method body is
checked: if the method is *not* declared `emission` but `_method_emissions`
finds the body reaches one, the error is *"`Svc.m` is declared plain, but
this implementation reaches `http_post`"* with the hint *"mark it
`emission fn m(...)` in service `Svc` … (G4)"*, plus a `WhyTrace` chain of
witness edges.

The async version, inserted in the same block:

> `Db.complete` is declared sync, but this implementation reaches async
> extern `http_post` — a sync method has no in-flight window (A1).
> hint: declare the operation `async fn complete(...)` in service `Db`, or
> move the suspending call out of this method.
> code=A1, category=async-propagation

Same evidence machinery, same why-trace shape (`WhyTrace(kind=
"async-propagation", …)` mirroring lower.py:3613-3615).

Note what this rule *is*: the service declaration is the upper bound on its
providers (the principle stated at lower.py:3593-3598), so asynchrony —
like emission-ness — is a declared property consumers can read off the
service, never something a provider smuggles in.

### v2: transitive coloring through named `fn`s

Module fns may call emission externs today, and the emission fixpoint
already computes transitive reach: `_emitting_fns` / `_emitting_capabilities`
(src/revl/emission_analysis.py:50-129) walk fn bodies with `_calls_in`
(emission_analysis.py:18-47), seed from emission externs, and close over
the static call graph, recording one witness edge per derived name
(`witness[caller] = callee`) with acyclic-by-construction chains
(`_witness_depth`, emission_analysis.py:147-160; `_emission_chain`, :162-173).

Async coloring is the same computation with a different seed:

```
_async_callables(fns, externs, witness) -> set[str]
  seed  = { ext.name | ext has "async": true }
  close = fn ∈ set  iff  its body calls a member of set
```

- Placed next to the emission pass — either a sibling in
  src/revl/emission_analysis.py (preferred: `_calls_in` and the witness
  helpers are reused verbatim) or a small `src/revl/async_analysis.py`.
- Invoked from `check_and_lower` where the emission fixpoint runs today
  (lower.py:2475-2476, `_emitting_capabilities(fns, externs, …)`).
- Every colored fn gets `"async": true` stamped on its IR entry (the dict
  built at lower.py:873-880), so emitters need no reachability analysis of
  their own.
- The method check above then tests membership of *any* callee in the
  colored set, not just direct extern calls — via the same body walk
  `_method_emissions` uses (lower.py:3600).

**Termination:** monotone closure over a finite name set; recursion and
mutual recursion are fine (a cycle either contains a seed-reaching member —
all colored — or none). The witness graph stays acyclic exactly as argued
at emission_analysis.py:147-152.

**Soundness statement:** after the pass, every admitted call edge whose
callee is async-colored originates inside an async context (an `async fn`
method or a colored fn). Emitters may therefore insert `await`
unconditionally at those call sites; no tier can drop a suspension, and no
`await` is ever emitted in a sync function.

**First-class values are refused, not widened.** The emission analysis
handles arrow-dispatch by assuming the worst (`*` capability — "must treat
the whole chain as possibly emitting", lower.py:3626-3640). Async cannot
use that trick: assuming-async would require the emitter to `await` calls
in contexts that may be sync, which is not writable. An arrow type
`(Str) -> Str` (docs/function-types.md) carries no color, so passing an
async extern or colored fn as a value would smuggle suspension past the
checker. Verdict: referencing an async callable in value position is a
compile error (*"an async extern has no arrow type — call it directly from
an async context"*). Coloring the arrow-type grammar is out of scope and
recorded as a rejected extension.

### Boundaries

- **Across services (the G8 boundary):** propagation stops at the service
  declaration. A consumer sees `async fn` in the service it binds to; the
  admission gate already treats an async flip as a breaking change to a
  service ("`m` becomes `async` — a consumer's call site …",
  src/revl/admission.py:246-249). So async-ness crosses component
  boundaries only by declaration, which is what keeps the fixpoint
  module-local and fast.
- **Component bodies:** activation bodies have their own `await` *step*
  with divert-boundary semantics (parser.py:1203-1214; ts lowers a
  component with await steps to an `async function*`,
  backends/typescript/emit.py:1029-1033). Letting an `emit http_post(...)`
  step suspend a fiber touches inertia/divert semantics (paper §4.3.2) and
  is deliberately **out of scope**; v1/v2 refuse with a hint to wrap the
  call in an async service operation.
- **Sync-only tiers:** the coloring checks are frontend and tier-
  independent — they run identically for a program that will only ever be
  emitted to go. The color then erases at emit (§2, family 2). This is
  intentional: a `.rvl` module must mean one thing before a tier is chosen.

## 4. IR change

One additive key on the extern node, mirroring the service-method spelling
(`**({"async": True} if m.async_ else {})`, lower.py:2583):

```python
# in _lower_externs, entry dict at src/revl/lower.py:1296-1303
entry: dict = {
    "name": decl.name,
    "class": decl.classification,
    "params": [...],
    "returns": decl.returns,
    "bodies": bodies,
    **({"async": True} if decl.async_ else {}),   # NEW
}
```

and (v2) the same key on colored fn entries (lower.py:873-880). Emitters
read it with `.get("async")` exactly as they read the method flag.

Documentation: the extern schema block in docs/backend-ir-v3.md:42-48 gains
the field, with the sentence "absent means sync; `class` must be
`emission`". Optionally (slice 7) the interchange extern record
(schema/revl-interchange-v1.schema.json:120-124, src/revl/interchange.py)
carries it too — additive, consumer-visible honesty about the boundary.

**IR version: stays 3 — flagged as a human judgment call.** The project's
policy is that additive sections bump the version "so a consumer that
predates the section refuses the whole document instead of silently
dropping" (lower.py:2545-2549). For this flag, silent dropping is
*correct* on go/java/rust (erasure is the defined semantics, §2) and
*loud* on the colored tiers: an old py emitter pastes an `await`-bearing
body into a plain `def` → `SyntaxError`; an old ts emitter reproduces
today's `tsc` error. Neither is a silent wrong answer — but the py failure
is boot-time, the exact class item 78 disliked. The alternative — emit
`ir_version: 4` when any async extern is present — costs a one-line
version-acceptance bump in all six emitters (e.g.
backends/typescript/emit.py:2308-2316). **Recommendation: keep 3;
confirm with a human before slice 2 lands.**

## 5. Per-tier lowering

### typescript (the target tier) — backends/typescript/emit.py

- **Extern decl** — `_emit_ts_externs` (:1886-1911): when
  `ext.get("async")`:

  ```ts
  export async function http_post(url: string, body: string): Promise<string> {
    // verbatim @ts body — may use await
  }
  ```

  i.e. `async ` prefix and `Promise<{_ts_v3_type(returns)}>` (declared-void
  externs become `Promise<void>`). Dedent handling (:1902-1907) unchanged.
- **Call sites** — the v3 first-class call branch of `_expr`
  (:423-445, `callee`/`args` shape at :441-445): when the callee is a `var`
  naming an async callable, emit `(await http_post(args))` —
  parenthesized so it stays atomic inside larger expressions
  (`f(x) + 1` → `(await f(x)) + 1`). Plumbing: `_Ctx` (:1156-1170), which
  already carries `extern_names` (:1160), gains `async_names: set[str]`
  (async externs + colored fns, read off the IR flags) and an
  `in_async: bool` context bit; `_method_body` already knows
  `method_is_async` (:757-758, set from the declaration at :853-856) and
  passes it down. Emitting an await while `in_async` is false raises
  `EmitError` — unreachable if the checker is right, and the honest crash
  if it is not (the contract style of :812-814).
- **Colored fns (v2)** — `_emit_ts_functions` (:1865-1883): fns with the
  IR flag emit `export async function …: Promise<T>`.
- **Reuse, not reinvention:** the async method prefix (:857), the
  await step (:807-815), the `Promise<T>` interface typing (:2135-2136,
  :2220-2221) and the lifecycle-driver await (:1995) all stay exactly as
  they are; async externs only add the *call-site* await and the extern
  signature form.

### python (the reference tier) — backends/python/emit.py

- `_emit_externs` (:1280-1299): `async def http_post(url, body):` when
  flagged; the `@py` body may then use `await` (aiohttp et al.) — or stay
  blocking (urllib), which is valid inside `async def` and observably
  equivalent (it just occupies the loop).
- Call sites: `(await http_post(url, body))` inside async provide methods —
  the method is already `async def` when declared `async fn`
  (:660-672), and the runtime metadata already records the flag for the
  driver (:1778-1779). Colored fns (v2) emit `async def` likewise.
- This *defines the reference semantics*: an async extern awaited in an
  async provide method behaves exactly like the awaited host async value A1
  already permits (the await lands, then control returns to the caller —
  the py comment at :607-609 and ts comment at :808-811 both state this).

### rust, go, java — erasure (no emitter change in v1)

The flag is ignored, exactly as these emitters already ignore the
method-level `"async"` flag (no `get("async")` in any of the three).
Extern bodies on these tiers must be written blocking — the same
requirement any `@rs`/`@go`/`@java` body has today. Their "no @rs body"
refusals (backends/rust/emit.py:2605, backends/go/emit.py:3177,
backends/java/emit.py:1799) still apply when a tier body is simply absent —
which is the *likely* state for an HTTP extern (the harness's is
`@ts`+`@py` only), so in practice these tiers usually refuse via the
existing message, honestly.

Future (out of scope, recorded): genuine rust coloring (`async fn` +
`.await`) can ride the `plugin_async` seam that component await steps
already use (backends/rust/emit.py:30, :500-501) once the rust tier grows
async service methods; java could offer `CompletableFuture<T>` signatures.
Neither is needed for correctness — only for non-blocking QoS.

### wasm — refuses

In the extern lowering path (backends/wasm/emit.py:785-793), before the
`no @wasm body` check: if the extern carries the async flag,

> extern `http_post` is `async`, which the wasm tier cannot express — the
> substrate's only async host seam is `await Job.run` (backends/wasm, docs
> header :36-38); give this extern a sync body on another tier or route it
> through a host service.

Same honest-refusal pattern as `Map.new` and non-Job awaitables
(backends/wasm/emit.py:914-924).

## 6. Interaction with existing machinery

- **`async fn` operations / A1:** the design deliberately makes "async
  extern" mean "the declared thing an async provide method awaits", so the
  two features compose instead of overlapping: parser gate for `await`
  statements (parser.py:1203-1214) unchanged; method prefixing unchanged;
  the only new interaction is the declared-sync-but-reaches-async check
  (§3), placed beside its emission twin (lower.py:3593-3644).
- **Emission machinery:** async externs are `emission` externs, so they are
  already inside `_emitting_capabilities`' seed set
  (emission_analysis.py:62-129) — G4 `emit`-marker discipline in setup mode
  (lower.py:2925-2934), emission-propagation into method declarations
  (lower.py:3599), capability scoping, and the G8 audit surface all apply
  unchanged. Async adds a *parallel* fixpoint with the same shape, not a
  modification of the emission one.
- **Admission:** no new dimension. Method async-ness is already
  compat-checked (a flip is breaking, admission.py:246-249); externs are
  module-internal and reach admission only through service surfaces.
- **Auto-mocks (src/revl/mocks.py):** a generated mock for an async extern
  must itself be async on colored tiers, or the inserted `await`s receive a
  non-thenable. The mock generator must read the same IR flag — required
  follow-through, slice 7.
- **Goldens (snapshot policy: regeneration allowed):** ts goldens under
  backends/typescript/golden/** are `tsc --noEmit`-gated
  (backends/typescript/package.json:11, tsconfig includes golden/**,
  backends/typescript/test_fr3_ts_emit.py:6-10) and regenerate via
  backends/typescript/scripts/regen-golden.py. A new async-extern fixture
  is *added*; existing goldens are byte-identical when no async extern is
  present (the flag is opt-in, so no regeneration storm). Reminder from
  the wave gap: `pytest tests/` does **not** run the per-backend golden
  suites — run each backend's own suite after its emit.py changes.

## 7. Implementation plan — ordered, landable slices

Every slice compiles, tests green, and lands independently. File sets are
disjoint unless marked; the two live agent groups (backend *runtimes*,
selfhost *lexer*) are untouched — this plan touches `backends/*/emit.py`
and frontend files only, never `backends/*/runtime.*` and never
`selfhost/`.

| # | Slice | Files | Depends on | Tests |
| --- | --- | --- | --- | --- |
| 1 | **Parser + AST flag.** Optional `async` between classification and `fn`; `ExternDecl.async_`. Parses and is *dropped* by lower (flag read in slice 2) — grammar-only land. | src/revl/parser.py (:254, :806-853) | — | parse round-trip + "async only after classification" error tests (new tests/test_async_extern.py) |
| 2 | **Frontend rules + IR flag.** Validity rules (§1: emission-only, no compensate); `"async": True` on the extern IR entry (lower.py:1296-1303); v1 call-context rules — allowed only in `async fn` provide methods, refusals for sync methods (the §3 diagnostic beside lower.py:3599), fn bodies, setup bodies, tests, undo slots, value position. Rejection fixtures. | src/revl/lower.py; examples/rejections/a1_async_extern_sync_method.rvl (naming precedent: examples/rejections/a1_await_in_method.rvl) | 1 | frontend rejection + acceptance tests; fixture wired into the REJECTIONS table |
| 3 | **ts emitter.** Async extern signature (`async …: Promise<T>`), call-site `(await …)`, `_Ctx.async_names`/`in_async`. Golden `async_http.ts` + a `tsc` end-to-end test in the test_fr3_ts_emit.py style reproducing the harness `http_post` shape. | backends/typescript/emit.py (:423-445, :1156-1170, :1886-1911); backends/typescript/golden/**, tests | 2 | vitest emitter suite + `npm run typecheck` (the exit test: harness-shaped `http_post` passes `tsc`) |
| 4 | **py emitter (reference semantics).** `async def` extern, call-site await in async methods. | backends/python/emit.py (:1280-1299 + call emission) | 2 | py golden + an asyncio-driven runtime test through an async provide method |
| 5 | **wasm refusal + docs.** The §5 wasm check; docs/syntax-2.0.md §5 paragraph, docs/backend-ir-v3.md:42 schema line, erasure documented for rust/go/java. | backends/wasm/emit.py (:785-793); docs/ | 2 | wasm EmitError test |
| 6 | **Full coloring (v2).** `_async_callables` fixpoint beside `_emitting_capabilities` (emission_analysis.py:50-129), `"async": true` on colored fn IR entries (lower.py:873-880), transitive method check, colored-fn emission on ts (:1865-1883) and py, first-class-use refusal. | src/revl/emission_analysis.py, src/revl/lower.py, backends/typescript/emit.py, backends/python/emit.py | 3, 4 | coloring unit tests incl. recursion; witness-chain diagnostic test; goldens |
| 7 | **Ecosystem follow-through.** Auto-mocks async-aware (src/revl/mocks.py); interchange `async` field (src/revl/interchange.py, schema/revl-interchange-v1.schema.json:120); tree-sitter grammar.js:168-181. | disjoint from all above | 2 | mock emission test; schema round-trip |

### Parallelization / collision map

- **Sequential spine:** 1 → 2 (both edit frontend files; one agent, or
  strictly ordered — slice 2 edits lower.py only, slice 1 parser.py only,
  so two agents *can* interleave, but 2 cannot start before 1's
  `ExternDecl.async_` exists).
- **Fan-out after 2:** slices 3, 4, 5, 7 touch pairwise-disjoint file sets
  (`backends/typescript/*`, `backends/python/emit.py`,
  `backends/wasm/emit.py` + docs, and mocks/interchange/tree-sitter
  respectively) — four agents in parallel, no collisions.
- **Slice 6 last:** it re-enters lower.py *and* both colored emitters, so
  it must wait for 3 and 4 to land (or be a continuation task for those
  same agents) to avoid emit.py merge collisions.
- **Live-agent guard:** nothing here touches `backends/*/runtime.*`,
  `selfhost/**`, or the lexer. The selfhost checker's extern parser
  (selfhost/checker.rvl:654) is a *filed follow-up*, not a slice.

### Test strategy (exit test first)

The feature's exit test is the harness's own failure, made a fixture:
`extern emission async fn http_post(url: Str, body: Str) -> Str` with the
real `fetch`-shaped `@ts` body, called from an `async fn` provide method;
`revl compile --backend ts` output must pass `tsc --noEmit` (slice 3's
test, wired like backends/typescript/test_fr3_ts_emit.py:54-70). Around it:
rejection tests for every §3 refusal (the checker-is-the-gate promise),
per-backend goldens, an asyncio round-trip on py (slice 4), and the wasm
refusal message test (slice 5). CI note: the fixture's `@ts` body should
await a `Promise.resolve(...)`-shaped expression in the *golden* (no DOM
`fetch` typing dependency), with the real-`fetch` variant living in
dogfood/ beside the harness.

## 8. Scope cut — smallest slice that greens the harness

**Slices 1 + 2 + 3.** That is: the `async` modifier parses (emission-only),
the IR carries the flag, calls are admitted only inside `async fn` provide
methods (no fn coloring — direct calls only, with the "phase 2" refusal for
helpers), and the ts emitter produces `async function …: Promise<string>`
plus awaited call sites. The harness's `http_post` then compiles and
passes `tsc`, and every other tier stays honest without any change:

- **py:** the flag is erased by the current emitter (it does not read it),
  and the harness's `@py` body is blocking urllib — sync `def`, sync call,
  correct behavior. Slice 4 upgrades py from "accidentally erasing" to
  "reference semantics" and should land in the same wave, but the harness
  is green without it.
- **rust/go:** already refuse via `no @rs body` / `no @go body`
  (backends/rust/emit.py:2605, backends/go/emit.py:3177) since the harness
  extern has no bodies for them — unchanged, honest.
- **wasm:** same `no @wasm body` refusal (backends/wasm/emit.py:785-793);
  slice 5's async-specific message is a politeness upgrade, not a
  correctness need.

The full six-tier version = the scope cut + slices 4-7, with slice 6
(transitive coloring) as the only conceptually new work — everything else
is plumbing a flag that slice 2 already put in the IR.

---

## Judgment calls a human should confirm before implementation

1. **IR version: additive flag on v3 (recommended) vs. bump to v4** when an
   async extern is present (§4). Policy tension with lower.py:2545-2549 is
   real; the recommendation leans on erasure-is-correct + loud failure on
   colored tiers.
2. **Erasure on the blocking tiers vs. refusal** (§2/§5). Erasure is
   consistent with how rust/go/java already treat the method-level flag and
   costs zero emitter changes, but it means an `async` extern with a
   *blocking* `@rs` body is silently fine — some may prefer the wasm-style
   refusal on rust until genuine `.await` support exists.
3. **`async` restricted to `emission` externs** (§1). `pure async` (a
   read-only fetch) has plausible use; the design refuses it to keep the
   coloring surface small. Confirm the fence.
4. **First-class refusal** (§3): async callables have no arrow type. This
   blocks e.g. passing `http_post` to a retry combinator; confirm that is
   an acceptable v1/v2 limit.
5. **py erasure window in the scope cut** (§8): acceptable to ship slice 3
   before slice 4, or require them to land together.
