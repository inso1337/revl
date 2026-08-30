# 388: caller-decided extern colour

Design note for revl-harness want H29: one host body shared by a SYNC and an
ASYNC extern, with the colour decided at the CALL SITE. This is design-first. It
changes no parser, typecheck, lower, or emit code; it records the shape of the
problem, how colour is fixed today, two mechanisms out, a recommendation, a
staged plan, and exit tests an implementation agent can pick up.

## The problem (measured)

`engine_run_sync` duplicates 184 lines of `@py` byte-identical to `engine_run`,
differing only in sync-vs-async colour (`docs/v2.0-roadmap.md:4108`, item 388).
The same lane already noted the neighbouring wound: the ~60-line seatbelt
mechanism is copy-pasted FOUR times, `engine_run` @py/@ts and `engine_run_sync`
@py/@ts (`docs/v2.0-roadmap.md:4078`, item 373). One caller (the async chat
provider, a `complete` provide method on `engine_model.rvl`, roadmap item 342 at
`docs/v2.0-roadmap.md:4016`) needs the awaitable form; another (the sync tool
surface, a `ToolRegistry.call`-style `emission fn`) needs the blocking form. The
language forces two extern declarations, so the body is authored twice.

The two externs and the seatbelt live in the downstream `revl-harness` repo, not
in `revl-core`; a full-repo search for `engine_run` finds no `.rvl` source here,
only the roadmap references above. This repo holds the cause (a language rule
about where extern colour is bound) and the fix lineage (items 90, 92, 115, 342,
which coloured the async boundary but never let one extern body wear two
colours). The line counts are the harness's measured figures; this note does not
quote the harness body.

Item 342 does NOT address this. It is the arrow analog of the same want (one
loop for a sync and an async caller) but it never looks at externs, confirmed in
detail under "Why item 342 does not reach this" below.

## Background: how an extern's colour is fixed today

An extern's colour is a property of the DECLARATION, carried as a single boolean,
lowered to one IR entry that holds one body, and emitted as one function. Nothing
downstream of the decl carries a per-call colour, so one extern name has exactly
one colour.

### Parser

`extern_decl` (`src/revl/parser.py:1098-1224`) parses the classification first
(`pure` / `acquire` / `emission` / `witnessed`, mandatory) and then an `async`
modifier in a small loop between the classification and the `fn` keyword
(`parser.py:1159-1169`), the same slot that accepts contextual `deferred`. So
`extern emission fn NAME(...)` yields `async_=False` and `extern emission async
fn NAME(...)` yields `async_=True`. Host bodies are captured at
`parser.py:1213-1220`, one `HostBody(backend, text, line)` (`parser.py:341-345`)
per `= @backend { ... }`. The colour lands on `ExternDecl.async_`
(`parser.py:372`), a single bool, next to `bodies: list[HostBody]`
(`parser.py:356`). One decl, one colour flag, one list of per-backend bodies.

### Typecheck / colour recording

Colour is not encoded into the extern's arrow type; externs share the fn
signature table (`parser.py:365-366`). The reachability record is built in
`src/revl/emission_analysis.py`: `async_externs` is `{decl.name: decl for decl
in program.externs if decl.async_}` (`emission_analysis.py:306-309`), a
name-keyed map. Because it is keyed by NAME, a name has exactly one colour.

### Lower

`async` is legal only on an `emission` extern (`src/revl/lower.py:1957-1987`
refuses `pure`, `acquire`, `witnessed`) and may not combine with `compensate`
(`1982-1987`) or `deferred` (`_check_deferred_extern`, `1661-1667`). The IR entry
(`lower.py:2016-2050`) collapses the per-backend bodies into one dict (duplicate
`@backend` bodies refused) and stamps `"async": True` only when the flag is set
(`lower.py:2031`, "Absent means sync"). The body text is stored ONCE
(`"bodies": bodies`, `lower.py:2027`); there is no per-colour duplication inside
lowering. Colour also seeds the phase-2 coloring fixpoint (`async_externs` /
`async_callables`, `lower.py:4936-4943`) so a module fn that reaches an async
extern becomes async-coloured.

### Emit

`_emit_externs` (`backends/python/emit.py:2118-2162`) decides the header on one
line, `kw = "async def" if ext.get("async") else "def"`
(`backends/python/emit.py:2143`), and writes the `@py` body verbatim, once, after
`textwrap.dedent(...).strip("\n")` (`emit.py:2155-2160`). Await at the call site
is decided by NAME membership, not by re-reading the body: `_PY_ASYNC_EXTERNS` is
built once as `{ext["name"] for ext in externs if ext.get("async")}`
(`emit.py:2877`), and each call site awaits iff the callee name is in that set
(`emit.py:789-797` for component methods, `emit.py:1895-1900` for module
expressions, membership tests at `emit.py:196` and `:198`). Item 115 made py
await async externs (`emit.py:2143` plus the seed at `:196`), matching ts.

### The load-bearing fact

Colour is a property of the DECL (one `async_` flag), the body is stored ONCE per
decl, and call-site await is keyed by the extern NAME. So a caller that needs the
blocking `def` form and a caller that needs the awaitable `async def` form
require two distinct extern names, each carrying its own copy of the identical
host body. There is no seam that carries a per-call colour, which is exactly the
gap item 388 names.

## Why the difference is only at the boundary

The measured byte-identity is itself a proof that `engine_run`'s body is
colour-agnostic. The emitter dedents, strips, and writes the body once
(`emit.py:2155`), choosing only the header colour. If the async body contained an
`await`, the byte-identical sync `def` twin would be a Python `SyntaxError` (an
`await` outside an `async def`) and `engine_run_sync` would not compile. It does.
So the 184 lines are await-free host code, and the ONLY colour delta is the
header (`async def` vs `def`) and the call site (`await f(...)` vs `f(...)`). The
`async def` is a coroutine-presenting wrapper over blocking work, which is what
lets a uniformly-awaiting async caller reach it. Two headers, one body, copied.
That is the whole of the 184-line waste.

Contrast a body that genuinely `await`s a host coroutine (an async spawn that
does `await proc.communicate()`). That body is NOT colour-agnostic; its sync
reading cannot exist as the same text. This distinction separates the two options
below: option (a) collapses a colour-agnostic body to one authored source, option
(b) shares text between two bodies that genuinely differ.

## Why item 342 does not reach this

342 (`docs/v2.0-roadmap.md:3978`, landed) is the arrow analog: a colour-
polymorphic fn, async solely because it calls its own async-typed callback
parameter, is monomorphized to a sync clone at a sync call site so one loop serves
both an async evolve path and a sync tool-call path. It does not touch externs,
for two reasons.

Mechanically, the monomorphize hook `_monomorphize_sync_callback_calls`
(`lower.py:4744-4791`) is called from `_lower_provide` and NOWHERE else
(`lower.py:5586-5593`; item 387 at `docs/v2.0-roadmap.md:4106` records this
exact confinement). It rewrites only `kind == "fn"` call nodes
(`lower.py:4759`), and the `colour_polymorphic` set is built solely from `fns`
entries (`lower.py:3538`). No part of it inspects an extern.

More deeply, 342 can monomorphize SOUNDLY because colour-polymorphism is a
CHECKED structural property of revl code: the fn is colour-polymorphic iff its
only async reach is its own async-typed parameters (`lower.py:3532-3547`), so the
sync clone provably has no residual suspension. An extern body is verbatim,
opaque host text; the compiler cannot look inside `@py { ... }`. So the extern
analog cannot DERIVE colour-agnosticism the way 342 derives it. It can only be
CLAIMED by the author or checked by a narrow syntactic guard. That is the honest
core of item 388, and both options below are different answers to it.

## Option (a): a colour-polymorphic extern, monomorphized per call-site colour

### Surface

One extern, one body, no fixed colour. A marker in the same modifier slot as
`async` says the extern may be emitted in either colour:

```revl sketch
extern emission fn|async engine_run(
    argv_json: Str, env_json: Str, cwd: Str, timeout_s: Int, sandbox_mode: Str
) -> Str = @py {
    # one body, written once; await-free host code
    ...
}
```

(A `poly` keyword in the `async`/`deferred` slot, `extern emission poly fn
engine_run(...)`, is an equivalent spelling; the `fn|async` form reads as "either
colour" and reuses the existing keyword.) The body is authored once.

### Colour resolution

The decl fixes no colour. The ENCLOSING caller's colour decides at each call
site. A call from a sync `emission fn` resolves to the sync emission (`def`,
unawaited); a call from an async `emission async fn` resolves to the async
emission (`async def`, awaited). This is 342's rule ("the caller's colour
decides") seeded by the call site's own colour instead of by an arrow argument.

### Lower (reuse 342's machinery, generalized in direction)

Do NOT make the coloring fixpoint polymorphic. Keep `_async_callables` running
over CONCRETE externs, and SPLIT the poly extern into concrete clones on demand,
mirroring `_synthesize_sync_monomorphs` (`lower.py:4794-4816`):

- During component lowering, each call site of a poly extern records the colour
  it needs into a shared `extern_colour_instances` map, the analog of
  `sync_monomorphs` (`lower.py:3550`, filled at `4779-4780`). A sync provide
  method requests the sync clone; an async one requests the async clone.
- After all components lower, synthesize the requested concrete extern entries
  into the externs list, deterministically name-mangled: the async clone keeps
  the ORIGINAL name (so `_PY_ASYNC_EXTERNS` and awaited call sites already
  resolve to it), the sync clone takes a `_sync_monomorph_name`-style suffix
  (`lower.py:4733-4741`). Both clones deep-copy the one authored `bodies` dict.
- Additive: a program that calls the extern in only one colour synthesizes only
  that clone, so parser, IR, and every golden stay byte-identical, exactly the
  342 additivity property (`lower.py:3609-3611`).

### Emit per tier

No emitter change on the coloured tiers. `_emit_externs` already renders `async
def`/`def` from `ext.get("async")` (`emit.py:2143`) and awaits by name membership
(`emit.py:2877`). Two synthesized entries with identical bodies emit as `async
def engine_run(...)` and `def engine_run_revl_sync(...)`, bodies byte-identical;
the async call site awaits the async name (`emit.py:789-797`), the sync call site
calls the sync name plainly. This reproduces today's two hand-copied functions
exactly, from one source body. On colour-erasing tiers (go/rust/java/wasm, where
suspension is not a function colour, `docs/design/async-extern.md` family 2) the
two clones would emit the same blocking function under two names; de-dup to one
emitted host function there, since the clones are identical once colour erases.

### A1 and awaited-vs-unawaited

Clean, because coloring runs over the concrete clones, not a polymorph. The async
clone is in `async_externs` / `_PY_ASYNC_EXTERNS`; its call site sits in an async
method and is awaited, which A1 permits. The sync clone is not in the async set;
its call site is unawaited, and the sync provide method reaches nothing async, so
A1 (`lower.py:3593-3644`) never fires. This is the same shape as 342, whose
monomorphize hook runs BEFORE the A1 admission (`lower.py:5586-5593`) precisely so
the lifted sync call has already cleared async membership.

### Effect on the 184-line dup

Deletes it entirely. One authored body, two synthesized headers, second
declaration gone.

### The honest hard part

Soundness rests on the body being colour-agnostic (await-free), which the
compiler CANNOT verify inside opaque host text (G8, item 24; the host body is not
sandboxed, `docs/design/329-untrusted-author-profile.md`). Three sub-answers,
in increasing cost:

1. A trust-me claim, honest-by-review. The poly marker is a claim the author
   vouches for, exactly as the classification itself is a claim
   (`emission`/`pure` are trusted, not proven) and as 373's reach clause is
   proposed to be. A body that awaits under `fn|async` is an authoring bug the
   diff review must catch, on the trust surface the classification already
   occupies.
2. A cheap per-backend syntactic lint: refuse a `fn|async` body whose text
   contains the backend's suspend keyword (`await` for py and ts). This catches
   the common mistake at compile time. State it honestly as a lint, not a proof:
   a body can suspend without the keyword (an event-loop `run_until_complete`
   call), and the keyword can appear inside a string literal.
3. The ordering wrinkle, the one place this is genuinely harder than 342.
   Coloring is computed once (`lower.py:3485`) BEFORE the component loop
   (`lower.py:3565`), but the call-site colour is known DURING it. Resolve by
   pre-seeding the async form in `async_externs` so awaited call sites resolve
   during lowering, then dropping the async clone in the post-pass if no async
   call site requested it, keeping only the sync clone. 342 never faces this
   because a colour-polymorphic fn is AUTHORED async, so the async original is
   always the surviving default; a poly extern has NO authored colour, so neither
   clone is privileged and the synthesis post-pass must choose which survives
   when only one colour is used. It is a real two-phase dance between the
   pre-lowering coloring fixpoint and the during-lowering call-site colour.

## Option (b): a host-body fragment/include (item 373's note)

### Surface

A named host-body fragment, written once, spliced into each colour's extern:

```revl sketch
fragment @py engine_seatbelt(cwd: Str, sandbox_mode: Str) {
    # the ~60-line jail, authored once
    ...
}

extern emission fn engine_run_sync(
    argv_json: Str, cwd: Str, sandbox_mode: Str
) -> Str = @py {
    include engine_seatbelt(cwd, sandbox_mode)
    result = subprocess.run(...)
    ...
}

extern emission async fn engine_run(
    argv_json: Str, cwd: Str, sandbox_mode: Str
) -> Str = @py {
    include engine_seatbelt(cwd, sandbox_mode)
    result = await asyncio.create_subprocess_exec(...)
    ...
}
```

Colour stays FIXED per extern; two declarations remain. What is deduplicated is
the shared BODY TEXT, not the colour.

### Resolve, lower, emit per tier

- Parser: a `fragment @backend NAME(params) { text }` declaration (a HostBody
  template, new next to `HostBody` at `parser.py:341`) and an `include NAME(args)`
  directive recognized inside a host body. No new colour concept. No such
  machinery exists today; the module-level `use` import (item 319) imports whole
  `.rvl` modules, not `@py`/`@ts` host-body text, so this is a new primitive that
  373 and 388 both propose.
- Lower: resolve each `include` by textual splice, per backend, binding the
  fragment params to the include args (the honest design choice is whether the
  args are typed and hygienic or a raw positional splice). Store the SPLICED body
  on the extern IR entry so emit is unchanged.
- Emit: no change. The entry already carries the fully-spliced body and
  `_emit_externs` writes it verbatim (`emit.py:2155`). One fragment per backend
  (`@py`, `@ts`), the same per-backend shape bodies already have
  (`parser.py:356`), so the same fragment can DRY across BOTH the colour axis and
  the backend axis (all four copies 373 names).

### A1 and awaited-vs-unawaited

Untouched. Two concrete externs with fixed colours; the async one awaits inside
its own tail, the sync one does not; each emits exactly as authored. No
monomorphization, no coloring subtlety. The include is a pre-emit text transform.

### Effect on the 184-line dup

Deletes the duplicated TEXT (the shared seatbelt and any shared body span) but
NOT the second declaration. `engine_run` and `engine_run_sync` both remain, each
a header plus its colour-specific lines plus `include`. For the byte-identical,
await-free case that is option (a)'s sweet spot, (b) still leaves two externs
whose bodies are `include full_body` plus a header, so it DRYs the text but keeps
two names.

### The honest hard part

It is a C-preprocessor for host bodies. A textual splice into verbatim, unchecked
host code (the G8 blind spot, `329`) inherits every macro hazard: hygiene (a
fragment param colliding with a body local), backend divergence (a `@py` fragment
and a `@ts` fragment that silently drift are exactly the security bug 373 fears,
and an include only lets them be written once, it does not FORCE lockstep), and
line-number provenance for diagnostics inside a spliced body. It DRYs the copy
without giving the compiler any more insight into the host text than it has today.

## Recommendation: (a) for the colour axis, (b) for the text/backend axis

Adopt (a) as the primary fix and build (b) alongside 373 for the orthogonal axis.
They are not competitors.

- (a) is the exact answer to the measured H29 want. The 184-line body is
  byte-identical and therefore (proved above) await-free and colour-agnostic, so
  (a) deletes the whole second extern, decides colour at the call site precisely
  as 342 does for arrows, and needs NO emitter change on the coloured tiers
  because the two synthesized concrete externs feed the existing `async
  def`/`def` plus name-keyed-await machinery unchanged. Adopt (a) with the
  trust-me claim plus the cheap `await`-keyword lint; leave a full proof of
  await-freedom explicitly out of scope, since the host body is opaque by G8.

- (b) is the right tool for the axis 373 raises. A body that is NOT
  colour-agnostic (genuinely awaits in the async form) cannot be a single poly
  body, and a body shared ACROSS BACKENDS (@py plus @ts) is a text-DRY problem,
  not a colour problem. For a seatbelt whose async and sync spawns genuinely
  differ (`create_subprocess_exec` vs `subprocess.run`) AND that spans two
  backends, (b) is the correct DRY, and it composes with (a): (a) collapses the
  colour axis for the colour-agnostic remainder, (b) collapses the shared-text
  and backend axis for the parts that genuinely differ.

For `engine_run` specifically: the roadmap measures the two externs as
byte-identical, so (a) alone deletes the 184-line dup outright. If a future
seatbelt hardening makes the async spawn genuinely `await` while the sync stays
blocking, the bodies stop being byte-identical, (a) no longer applies to that
span, and (b) carries the shared seatbelt prefix while two thin colour-specific
tails remain. Build (a) first (it clears the measured want); build (b) with 373
when the first genuinely-await-differing shared body appears.

## Migration for engine_run / engine_run_sync

In the harness (no revl-core change lands the harness edit; the compiler change is
option a's staged plan below):

1. Replace the two externs with one colour-polymorphic `extern emission fn|async
   engine_run(argv_json, env_json, cwd, timeout_s, sandbox_mode) = @py { ...one
   body... }`, plus the `@ts` body written once.
2. Point the async chat-provider path (the `complete` async provide method in
   `engine_model.rvl`, roadmap item 342 at `docs/v2.0-roadmap.md:4016`) at
   `engine_run`; it resolves to the async clone, awaited.
3. Point the sync tool-surface path (the `ToolRegistry.call`-style sync `emission
   fn`, the same caller 342 unblocked for arrows) at `engine_run`; it resolves to
   the sync clone, unawaited, no A1.
4. Delete `engine_run_sync` and its 184-line @py/@ts copies. The synthesized
   clones reproduce today's two emitted functions byte-identically; verify against
   the pre-migration goldens.
5. For the seatbelt specifically (373): if and when the async and sync spawns
   diverge, lift the shared ~60 lines into an `@py`/`@ts` fragment (option b)
   included by whatever externs remain, so the jail is authored once and cannot
   drift silently between the two spawns.

## The A1 and emission-semantics interaction (consolidated)

- A1 (`docs/rejections.md:280-305`; diagnostic at `lower.py:3593-3644`,
  `code=A1, category=async-propagation`) refuses a sync method or extern reaching
  an async op, because an active component body has no suspension window to
  divert. Option (a) never trips A1: the sync call site resolves to a CONCRETE
  sync clone (not in `async_externs`), so the sync provide method reaches nothing
  async; the async call site resolves to a CONCRETE async clone inside an async
  method, which A1 permits. This mirrors 342, whose monomorphize hook runs before
  the A1 admission (`lower.py:5586-5593`) so the lifted sync call has already
  cleared async membership.
- Awaited-vs-unawaited is decided by NAME membership in `_PY_ASYNC_EXTERNS`
  (`emit.py:2877`) at the call site (`emit.py:789-797`, `1895-1900`), not by the
  body. Because (a) produces two concrete names, the existing per-name await
  decision is correct with zero emitter change: await the async clone, call the
  sync clone plainly. Item 115 already made py await async externs (`emit.py:2143`
  plus the seed at `:196`), so the async clone behaves exactly as `engine_run`
  does today.
- Option (b) leaves emission semantics entirely untouched: two fixed-colour
  externs, each awaited or not exactly as authored; the include is a pre-emit text
  transform with no bearing on coloring.

## Byte-identity guarantees

- Additive. No poly extern and no fragment means parser, IR, and every backend
  golden are byte-identical, carrying the 342 additivity property
  (`lower.py:3609-3611`) to externs.
- Post-migration. Option (a)'s async clone equals today's `engine_run` golden and
  its sync clone equals today's `engine_run_sync` golden (both after the emitter's
  dedent and strip, `emit.py:2155`). Option (b)'s spliced body equals today's
  hand-copied body. Both are verified by diffing against pre-migration goldens.

## Staged implementation plan (option a)

- Stage 1 (parser). Accept the poly colour marker on `ExternDecl` in the
  `async`/`deferred` modifier slot (`parser.py:1159-1169`), carrying a new
  `colour_poly: bool` next to `async_` (`parser.py:372`). Exit: a poly extern
  parses; a plain extern is byte-identical; `poly` combined with `async` or
  applied to a non-`emission` extern is refused in lower.

- Stage 2 (lower, identify and validity). A poly extern is emission-only, not
  deferred, and mutually exclusive with a fixed `async` (reuse the emission-only
  refusals at `lower.py:1957-1987`). Do NOT emit an IR entry for a poly extern
  that no call site instantiates; register it in a poly-extern table. Exit: a poly
  extern with no call site emits nothing (additive); validity refusals land with
  the existing emission-only messages.

- Stage 3 (lower, call-site resolution). During component lowering, at each call
  of a poly extern, record the enclosing method's colour into
  `extern_colour_instances[(name, colour)]`, reusing the `sync_monomorphs`
  pattern (`lower.py:3550`, `4779-4780`). Rewrite the call to the concrete clone
  name (async clone keeps `name`, sync clone takes the `_sync_monomorph_name`
  suffix, `lower.py:4733-4741`). Exit: a poly extern called from both a sync and
  an async provide method records two instances and two rewritten call sites.

- Stage 4 (lower, synthesize). After all components lower, materialize the
  requested concrete extern entries, deep-copying the one authored body into each
  and stamping `"async": True` on the async clone only, mirroring
  `_synthesize_sync_monomorphs` (`lower.py:4794-4816`). Resolve the ordering
  wrinkle (pre-seed the async form in `async_externs` for awaited-call-site
  resolution, drop the unused clone in the post-pass). Exit: two concrete externs
  in the IR with identical bodies, differing only in the `async` key.

- Stage 5 (emit, py then ts). No change expected. Verify `_emit_externs`
  (`emit.py:2143`) and `_PY_ASYNC_EXTERNS` (`emit.py:2877`) render the two clones
  and that the async call site awaits while the sync does not. Exit: golden py and
  ts output for a two-colour poly extern equals two hand-written externs
  byte-for-byte.

- Stage 6 (colour-erasing tiers). De-dup the two clones to one emitted host
  function on go/rust/java/wasm, where colour erases
  (`docs/design/async-extern.md`, family 2). Exit: no duplicate host function on a
  sync-only tier; the conformance table (`docs/conformance.md`) stays green.

- Stage 7 (docs and guard). Document the poly extern as a claim the author is
  trusted to honour (the body is colour-agnostic, await-free), add the cheap
  per-backend `await`-keyword lint, and state that proving colour-agnosticism is
  out of scope by G8 (`329`). Exit: `test_doc_examples` stays green (this note's
  proposed-syntax blocks are `sketch`); the lint refuses a poly `@py` body
  containing `await`.

Option (b)'s staging (parser fragment decl and include directive, lower textual
splice per backend, emit unchanged, hygiene and provenance handled, goldens
byte-identical to hand-copied) is deferred to item 373's own design, since the
fragment primitive is 373's proposal and serves the reach-clause lane too.

## Exit tests

For option (a), the language addition:

- Additive: a program with no poly extern and no fragment is byte-identical across
  parse, IR, and every golden (the 342 additivity test, extended to externs).
- One poly extern `extern emission fn|async e(...) = @py {…}` called once from a
  sync `emission fn` and once from an async `emission async fn` synthesizes two
  concrete externs; the sync call site emits unawaited, the async awaited; py and
  ts goldens each equal the two-hand-written-extern baseline byte-for-byte.
- A1 is untouched: the sync caller of the poly extern is NOT refused (it reaches
  the sync clone) and no `await` lands in the sync-emitted function; the async
  caller IS awaited.
- Migration: after replacing `engine_run`/`engine_run_sync` with one poly extern,
  the emitted py and ts for both callers diff-clean against the pre-migration
  goldens, and `engine_run_sync`'s 184-line copy is gone from source.
- Guard: a poly `@py` body containing `await` is refused by the lint (a
  colour-agnostic body cannot suspend); a body without it is accepted.
- Colour-erasing tier: go and rust emit ONE host function for the poly extern, not
  two.
- `test_doc_examples` stays green: every proposed-syntax block in this note is
  marked `sketch` and must not compile until the feature lands, at which point the
  worked examples are promoted.

## The honest hard part (consolidated)

An extern's host body is verbatim, opaque, unchecked host text (G8, item 24;
`329`). 342 can monomorphize soundly because it DERIVES colour-polymorphism from a
checked structural property of revl code; the extern analog has no such foothold,
because the compiler cannot read `@py { ... }`. So caller-decided extern colour
must rest on an AUTHOR CLAIM that the body is colour-agnostic (await-free),
verifiable only by the same honest-by-review discipline that already backs the
classification and 373's reach, plus at most a cheap syntactic lint. The design
does not pretend to prove what it cannot see; it makes colour a call-site decision
over a body the author vouches is colourless, and routes the genuinely
colour-bearing or cross-backend remainder to option (b)'s explicit, per-colour,
per-backend fragment. The one place it is strictly harder than 342 is the missing
authored default: a colour-polymorphic fn is authored async and monomorphized back
to sync, so the async original always survives as the default; a poly extern is
authored with NO colour, so the synthesis post-pass must choose which concrete
clone survives when only one colour is used, a two-phase dance between the
pre-lowering coloring fixpoint and the during-lowering call-site colour.
