# Design: idempotent inverse verification (item 309)

Design note for roadmap item 309 (`docs/v2.0-roadmap.md:3889`): an
`idempotent` effect declaration, the `undo(undo(state)) = undo(state)`
requirement, and an idempotency key for external systems
(`inventory.release(reservation, idempotency_key=reservation.id)`). This is
design-first, part of the 243-261 product-vision pass. It changes no compiler
code; it records the surface, the honest verification ledger (what is static
proof, what is a test, what is a declaration), the concrete payoff in the
recovery model, the diagnostic, the reconciliation with 243/247/294/44/125,
and a staged plan with exit tests.

## The one thing to get right

G7 guarantees LIFO-complete teardown ONCE, in a clean in-process unwind.
Nothing guarantees an inverse is safe to run TWICE, and twice is exactly what
the crash-recovery model already does:

- `revl recover` roll-back re-issues every undischarged transactional inverse
  from its WAL descriptor (`src/revl/recovery.py:526-553`). Recovery itself
  can crash mid-replay and be re-run; the second run re-issues inverses the
  first run already applied. There is no per-inverse "already ran" fence.
- Abort replay in-process has the same shape: 243 correctness rule 5 says
  "abort replay can crash mid-way and `revl recover` replays again; inverses
  must be idempotent (or replay checkpointed)"
  (`docs/design/243-witnessed-externs.md`).
- The 243 Slice-2a LIFO argument leans on the same assumption in prose: the
  stdlib fs inverses are "idempotent-and-total (a second replay is a no-op)".
- 247 Decision 5 states the requirement for compensations and defers the
  mechanism here by name: "a typed idempotency key is item 309's territory"
  (`docs/design/247-compensate.md`, open question 3). The recovery code
  already prints the hint at the residue site: "Verify it was offset, or
  carry an idempotency key" (`recovery.py:579`).

So the system's soundness under crash-retry currently rests on an UNDECLARED
idempotency assumption, honored by the first-party stdlib bodies and hoped
for everywhere else. A non-idempotent inverse (an apply-delta shape:
`counter.increment(n)` as the undo of `counter.decrement(n)`, an
append-based restore, a refund issued per call) double-applies on a
crash-retry, and today nothing names, tests, or fences that.

Item 309 turns the assumption into a first-class, declared, audited, and
fault-sweep-tested property, and cashes it where it pays: recovery may
auto-replay a declared-idempotent inverse freely, and must fence or defer a
non-idempotent one. In one line: **G7 gives LIFO once; 309 makes twice
safe, and says honestly for which inverses it cannot.**

## What already exists (the precedent is stronger than expected)

`idempotent` is already a revl word, shipped by item 44
(`docs/delivery-semantics.md`):

- **Grammar.** A modifier on a service-method `emission`, sibling of
  `commutative` (`src/revl/parser.py:8`, `parser.py:49`, keyword at
  `lexer.py:35-37`). Only an emission may claim it; a plain `fn` claiming it
  is refused.
- **IR.** Lowers to `"idempotent": true` on the method
  (`lower.py:4725`), property tier `ir_version: 3`.
- **Runtime meaning.** The py reference runtime auto-retries a
  `TransientError` iff the emission is declared idempotent (budget 3);
  a non-idempotent emission gets exactly one attempt
  (`backends/python/runtime.py`, per `delivery-semantics.md`).
- **Honesty register.** Stated as the author's claim about their server
  (`f(f(x)) == f(x)`), shape-checked, never behavior-proved. The OpenAPI
  importer writes it only for the verbs RFC 9110 defines idempotent (PUT,
  DELETE), as imported evidence (`src/revl/import_openapi.py:80-85`).

What does NOT exist yet, and is 309's actual surface area:

1. The extern modifier slot does not accept `idempotent` (externs take
   `async`/`deferred`/`fn|async` between classification and `fn`,
   `parser.py:1385-1401`). External effects are declared as externs, so the
   shipped claim cannot even be spelled where 309 needs it.
2. Nothing can claim idempotency of an INVERSE (`undo` slot on an
   `acquire`/`witnessed` extern), which is the property recovery actually
   leans on.
3. There is no idempotency KEY: no way to name the argument a remote system
   dedups on, which is the only mechanism that makes an external effect
   idempotent by construction rather than by trust.
4. Nothing tests any of it: the fault sweep (item 125, `src/revl/fault.py`)
   and the inverse-roundtrip dossier (`fault.roundtrip_dossier`,
   `registry.py:55-67`) run every inverse ONCE and assert a clean ledger;
   no unit runs an inverse twice.
5. Recovery does not consume the property: roll-back replays every
   undischarged inverse unconditionally, and roll-forward "never auto-fires
   an owed emission" (`recovery.py:357`) even when firing twice would be
   provably harmless.

## Surface (question 1)

### Decision: grammar, not manifest

The 294/411 crux table settles placement by which party knows the fact.
Whether `restore(w)` can run twice is a fact about the inverse's host body
and about the remote API it talks to; that is author knowledge, exactly like
item 44's emission claim and item 373's `confined:` parameter role. The
manifest keeps its 411 role (enforcement mapping); the declaration is
source. This also keeps one word with one meaning: 309 REUSES the shipped
`idempotent` modifier rather than minting a rival spelling.

### 1a. The inverse-side declaration: `undo idempotent`

```revl sketch
extern witnessed[fs] fn rm(path: Str) -> Result[FsWitness, FsError]
    undo idempotent restore(result)

extern acquire fn open(url: Str) -> Pool
    undo idempotent close(result)
```

- `idempotent` is ALREADY a reserved keyword: item 44 put it in the lexer's
  KEYWORDS set (`lexer.py:35-37`, with the delivery comment) and the
  service-method parser matches it as a `kw` token (`parser.py:1681`). No
  program can use `idempotent` as an identifier today, so there is no
  compatibility story to defend and no contextual keyword to introduce.
  `undo idempotent restore(result)` has no ambiguity: an inverse fn named
  `idempotent` is impossible (the word is reserved), so the parser change is
  just accepting the existing kw token in the undo slot. `idempotent(key: p)`
  in the extern modifier slot is likewise unambiguous: the confined reach
  clause is consumed first, so a bracket after `idempotent` can only be the
  key role.
- It attaches to the INVERSE, not the forward effect, because that is what
  the claim is about. Placing it in the extern classification slot
  (`extern witnessed[fs] idempotent fn rm ...`) was considered and rejected:
  it reads as a claim about `rm` (the forward mutation), which is a different
  and here-irrelevant property, and for an `emission` extern the same slot
  already means forward re-delivery (1b below). One word, two positions, each
  scoping the claim to the call it precedes.
- Meaning: `undo(undo(s)) = undo(s)` on the observable boundary state, for
  the same witness. Running the declared inverse a second time with the same
  bound `result` leaves the world exactly as the first run left it.
- IR: `"undo_idempotent": true` on the extern node, next to the 243
  descriptor fields (`entry_kind`, `revertible`, `ok_conditional`), and
  carried into the WAL discharge-descriptor so RECOVERY sees it in a fresh
  process (the descriptor, not the source, is what `recover` reads).
- Inference is refused as a design option: a host `undo` body is G8-opaque;
  revl cannot look inside it (see the verification ledger). Declaration or
  nothing, stated as such.

### 1b. The external-effect declaration: `idempotent` in the extern modifier slot, with a key role

Two strengths, deliberately distinct:

```revl sketch
// trust-me claim (item 44's register, now spellable on an extern):
extern emission[http] idempotent fn put_config(key: Str, value: Str) -> Unit

// by-construction claim: the remote dedups on the named argument
extern emission[inventory] idempotent(key: reservation_id)
    fn release(reservation_id: Str) -> Unit
```

- Bare `idempotent` in the extern modifier slot (same slot as
  `async`/`deferred`, combinable with them) is exactly item 44's claim
  extended from service methods to externs: the author asserts the server
  treats re-delivery as delivery. Shape-checked (emission-only), audited,
  trusted.
- `idempotent(key: <param>)` names WHICH declared parameter carries the
  idempotency key. This is the item-373 `confined:` precedent
  (`parser.py:1364-1365`, the parameter-role bracket in the same position):
  a role annotation on the signature, checked against the parameter list.
  The checker verifies the named parameter exists and has a serializable
  scalar type (`Str` or `Int`); the WAL boundary descriptor records the
  key's VALUE per call. The roadmap's spell
  `inventory.release(reservation, idempotency_key=reservation.id)` is this
  form at the call site: the author routes `reservation.id` through the
  declared key parameter, and every retry (runtime TransientError retry,
  recovery re-issue, operator replay) carries the same value, so the remote
  dedups. This is the real mechanism for external systems: the key makes
  the effect idempotent BY CONSTRUCTION, conditional only on the remote
  honoring its documented dedup contract (still a trust boundary, named in
  the ledger, the same way 411 trusts Docker).
- The keyed form also extends to service-method emissions
  (`emission idempotent(key: k) fn put(...)`) for symmetry with item 44;
  same checks.
- A remote-touching reversal is necessarily a COMPENSATION, never an
  inverse: 243 rule 3 keeps inverses non-emission, and the keyed form is
  emission-only, so you do not write a keyed inverse, you write a keyed
  `compensate`. A `compensate` expression whose emission target is declared
  idempotent is thereby retry-safe, with the register carried honestly: at
  bare strength "retry-safe" is trust-registered (the author's item-44
  claim, nothing more), while the keyed form makes the recovery re-attempt
  dedup-safe by construction, conditional on the remote's dedup contract
  (reconciliation below).

### 1c. Reconciliation with 294: the key is a 373-style parameter role, NOT a capability valuation

The brief's instinct ("`idempotency_key=` is literally a 294-style
parameter") is half right and the half matters. A 294 valuation
(`fs.write(path="/tmp")`) is a STATIC point in a partial order under a
capability token, compared at admission to prove narrowing; its values are
literals or config symbols, and it never flows to the host as data. An
idempotency key is per-call DYNAMIC data (`reservation.id`, different every
call) whose whole job is to flow to the host; it has no order, narrows no
authority, and can never participate in the attenuation fold. Forcing it
into the capability grammar would either freeze it to a static literal
(useless) or smuggle dynamic values into `cap_order` (breaking 294's closed
registry and its no-widening argument). So: the key rides the extern
SIGNATURE as a declared parameter role (373's machinery), the capability
token stays 294's. The two compose without touching: an effect can be both
`emission[inventory.release(host="stock.internal")]` (294, where it may
cross) and `idempotent(key: reservation_id)` (309, how it dedups). One
sentence of shared spelling is still honored: both keep the rule that a
declared name must resolve against the parameter list, fail-closed at parse.

## Verification: the honest ledger (question 2)

What `undo(undo(state)) = undo(state)` can actually be, per register. This
table is the 411/294 honesty discipline applied to 309; the audit surface
must print the register per inverse, so no reader mistakes a claim for a
proof.

| inverse kind | static proof | test | declaration |
|---|---|---|---|
| host `undo` body (today: all of them) | NO. G8-opaque; revl cannot prove anything about the body | YES: fault-sweep double-undo (below) exercises the DESCRIPTOR against a world model, and a host-adapter run exercises the real body | YES: `undo idempotent`, carried on IR + WAL + audit |
| external emission, bare `idempotent` | NO (remote behavior) | PARTIAL: item 37-style recording roundtrips can probe it, not prove it | YES: the item-44 register, unchanged |
| external emission, `idempotent(key: p)` | shape only: `p` exists, scalar-serializable, threaded to the WAL descriptor (parse/check refusal on violation) | YES: the sweep asserts the SAME key value on both issues of a re-run, and the collision twin asserts distinct logical operations carry distinct values | by construction, conditional on the remote honoring its dedup contract INCLUDING ITS RETENTION WINDOW (a named trust boundary, like 411's runtime enforcer; a recovery re-issued after the remote forgot the key re-applies) |
| native inverse expressed in revl (item 244 `stdlib/fs.rvl` bodies, future) | RESTRICTED YES: a body in restore-to-recorded-value form (every write is `set(target, w.field)`, no reads of current state, no deltas, no appends) is last-writer-wins, hence idempotent by construction; the checker can verify THAT SHAPE syntactically | YES, same sweep | not needed where the shape check passes; `undo idempotent` still spellable to state intent |

Scope of the sweep, stated plainly so the table cannot be over-read: the
double-undo instruments catch referent-cardinality divergence and VALUE
divergence, and nothing else. External-emission divergence (a refund that
fires twice at a real remote) is invisible to the model world by
construction; it is covered only by the keyed register and the mock-remote
key tests below, never by the sweep. A green sweep says the descriptor's
replay is value-stable against the model, not that the world's remotes
dedup.

Three honest sentences the docs must carry, mirroring 294's:

1. **revl never proves a host inverse idempotent.** The declaration is the
   author's claim; the sweep is evidence against a model (and against the
   real host only when a `World` adapter over the real system is supplied);
   only the restricted native form is a static proof, and it is narrow.
2. **The key is the mechanism, the remote is the enforcer.** With
   `idempotent(key: p)` revl guarantees the same key VALUE reaches every
   re-issue (checkable, tested); whether the remote dedups on it is the
   remote's contract, including its RETENTION WINDOW: a recovery run days
   after the crash can re-issue a key the remote has already forgotten, and
   the remote then re-applies. The register is "conditional on the remote
   honoring its dedup contract, including its retention window", and
   `revl audit` carries the window where the import evidence names one
   (OpenAPI importers increasingly do). The audit prints
   `idempotent: declared` vs `idempotent: keyed` vs
   `idempotent: shape-proven` so the three never blur. The key's own
   failure mode is UNDER-apply, not double-apply: two distinct logical
   operations issued under one key value are dedup'd by the remote into
   one application, and the leak is silent because every retry-safety
   report reads clean (open question 3 and the collision twin below).
3. **A test that passes is evidence, not a guarantee.** The double-undo
   sweep catches divergence (a non-idempotent inverse WILL fail it, exit
   tests below); passing it upgrades nothing to a proof. This is the same
   register `delivery-semantics.md` uses for item 44 and the same "checked,
   not asserted, and honest about the model" voice as
   `docs/crash-recovery.md` §6.

### The test: extend the fault sweep and the roundtrip dossier

The property in operational terms: **re-applying an inverse's WAL
descriptor to the post-rollback world changes nothing.** That form is
chosen deliberately: it is exactly what a crash-retried recovery does, so
the test exercises the real re-run path, not a synthetic one. Three
additions:

A precondition the current instruments do not meet, fixed first: they are
value-BLIND, so the exact classes 309 exists for would pass. The roundtrip
fingerprint (`fault.py::_outstanding`) folds the trace into referent SETS,
carrying keys and never values, so a delta-shaped inverse (the design's own
counter-increment headline) moves a value twice and the fingerprint never
sees it. `DictWorld.apply_inverse` (`recovery.py:102`) pops referents and
dict-overwrites a fixed key, both idempotent by construction, so the model
world CANNOT represent non-idempotency: a double-refund passes against it.
Left as-is, a falsified `undo idempotent` declaration sails through the
sweep and `replay: free` rests on unearned evidence. So:

1. **Value digests in the trace and fingerprint.** The trace vocabulary
   extends from bare referent lines to value-carrying lines
   (`<tag>.insert <key> = <hash>`, a short stable digest of the applied
   value), and the fingerprint folds the digests, not just the key set. A
   second application of a delta-shaped inverse moves the digest and the
   fingerprint diverges; a restore-to-recorded-value inverse re-applies to
   the same digest and it does not.
2. **Roundtrip double-undo round** (`fault.py`, `_drive_roundtrip`): after
   the existing dispose-and-fingerprint round, re-issue the recorded
   reconstructible inverse ops against the world/ledger once more and
   fingerprint again with the value-carrying form; the round passes iff
   `final2 == final` over digests. The round runs against a world that
   models VALUES, not just referents: an extension of `DictWorld` that
   stores the per-key digest and applies delta ops as deltas, so a
   non-idempotent shape can actually diverge in the model. The dossier
   (`_roundtrip_dossier`) gains a per-component `doubleUndo` facet, and the
   registry's `inverse-roundtrip.json` evidence (`registry.py:55-67`)
   carries it, so the gauntlet grade can require it.
3. **Recovery-twice unit, with crash-mid-replay cuts** (sweep level, item
   125 family): run `revl recover` against a mid-abort WAL, snapshot the
   `World`, run `recover` again on the same WAL, assert the world is
   unchanged and no new residue appears. Two complete passes are the easy
   case; the real condition is a PARTIAL first pass. `fault.py` is a
   fault-point engine, so the unit gains crash-after-k injection points:
   for each k in 0..n, cut the first pass after k of n inverses, then run
   a full second pass, and assert convergence to the once-through result.
4. **Key-stability assertion**: for a keyed emission re-issued by recovery
   or retried by the runtime, assert both issues carried the identical key
   value (read from the WAL descriptors). Its twin is the COLLISION
   assertion: two distinct logical operations must carry distinct key
   values (exit tests below name under-apply as the failure mode).

The claim the crash-cut unit actually needs is SEQUENCE-level, not
per-inverse: per-inverse `undo(undo(s)) = undo(s)` composes to convergence
only when inverses over OVERLAPPING referents are order-stable under
re-interleaving, and 243 Slice 2a point 5 already names the
order-sensitive counterexample (`mv a b; mv b c`). The claim stated
outright: **any crash-cut sequence of passes, each a prefix of the same
LIFO order and the last one complete, converges to the once-through
result.** For the restore-to-recorded-value shape this holds by a
last-writer-wins argument: every inverse writes an absolute recorded value
and reads nothing from the current state, every pass replays the same
reverse-seq order from the same WAL, so whatever prefixes ran before, each
referent's final value is the one written by the last inverse touching it
in the completing pass, which is the once-through result. Shapes outside
that form (a real `mv` chain) get no sequence-level claim from 309; they
ride the fenced path.

A declared-idempotent inverse that FAILS the sweep is a hard fail (the
claim is falsified by evidence; same severity as a residue-bearing fault
point). An undeclared inverse that fails the double-undo round is reported
as `replay: fenced` information, not a fail (it never claimed the
property); the recovery model below is what makes that state safe.

## Where it buys safety: recovery (question 3)

The concrete payoff, tied to the shipped model (item 322 made the WAL
six-tier; item 413 hardens its integrity; neither changed the replay
policy):

### 3a. Roll-back: free replay vs fenced replay

Today `_roll_back` re-issues every undischarged transactional inverse, and
a re-run of recovery re-issues them again (`recovery.py:526-541`). New
policy, keyed off `undo_idempotent` in the descriptor:

- **Declared idempotent: replay freely.** Recovery re-issues it on every
  run; a crash-retried recovery is safe by the declared property. No
  bookkeeping. This is the common case (every stdlib fs inverse) and it
  makes `revl recover` itself idempotent over the declared subset, which is
  the property a supervised/systemd-restarted recovery actually needs.
- **Not declared: at most one attempt, WAL-fenced, on EVERY apply path.**
  Before applying, the applier appends a `replay-attempted` fence record
  (fsync'd, consume-before-fire discipline, the same ordering
  `_consume_grant` uses for grants); a later recovery run that finds the
  fence does NOT re-apply. The fence mechanics are what carry the
  guarantee: fence-fsync-before-apply means at-most-once-by-recovery, a
  torn fence line implies the apply never ran (the append completes before
  the attempt starts, so a torn record is discarded and the attempt may
  proceed), and a forged fence is exactly the record class item 413's hash
  chain exists for (a forgery could suppress a needed replay or force a
  double one; routed there, reconciliation below).

  "Every apply path" is load-bearing, and it is the part a recovery-only
  fence gets WRONG: the in-process ABORT applies inverses too (Phase 1),
  and without a durable per-inverse record there, the headline scenario
  double-fires. Concretely: a witnessed effect with an undeclared
  non-idempotent inverse; an in-process abort begins, Phase 1 applies the
  inverse (the refund fires), the process dies before anything durable
  marks it; `revl recover` finds the seq undischarged and applies it
  AGAIN. Two applications, under the policy whose whole point is one. Two
  fixes were weighed: (a) the abort path fsync-appends the same fence
  record before each Phase-1 apply of an UNDECLARED inverse, or (b) a
  single `teardown-started` record before Phase 1, after which recovery
  treats every undeclared undischarged inverse as possibly-attempted and
  DEFERS without attempting. **The design takes (a).** It is per-inverse
  precise: recovery can still attempt the inverses Phase 1 provably never
  reached, where (b) surrenders all of them to human-finish after any
  abort-then-crash, and (b)'s single record buys only fewer fsyncs on a
  path (abort with undeclared inverses under WAL recording) that is
  already paying fsync per discharge. Declared inverses need no fence on
  either path, which is a further payoff of declaring: the abort of a
  fully-declared composition writes zero extra records.

  The residue wording is register-honest about what the fence knows. After
  a crash BETWEEN fence and apply, the truth is "fenced before attempt;
  outcome unknown", not "attempted once": the fence proves the attempt was
  about to start, never that it ran. The residue proof reports
  `fenced-before-attempt, outcome unknown, will not re-run` and defers to
  a human with the referent named. Fenced is honest: at most one unfenced
  attempt ever happens across abort and any number of recovery runs, a
  second cannot be proven safe, so it is refused automatically and handed
  over, in the existing `_residue_proof` voice. (The earlier draft's "one
  attempt is safe, that is today's single-run behavior" was true only when
  no abort ran; the abort fence is what makes it true unconditionally.)

So the answer to the brief's question is yes, and precisely: **declaring
`idempotent` is what lets recovery auto-replay an inverse it would
otherwise fence after one attempt and defer to a human on retry.** The
non-idempotent path degrades gracefully instead of double-applying.

### 3b. Roll-forward: the owed-emission rule gets its first exception

`recover never auto-fires an owed emission` (`recovery.py:357`, stated as
v1). The v2 rule: an owed deferred emission whose extern is
`idempotent(key: p)` MAY be auto-fired by recovery, because the descriptor
carries the key and the fire is dedup-safe even if the pre-crash flush
actually landed (the exact ambiguity that motivated the v1 rule). Owed
emissions that are bare-`idempotent` (trust-me, no key) remain
human-finish by default, with a policy knob (item 33-style) for operators
who accept the claim; unmarked ones remain human-finish unconditionally.
The window proof (`_window_proof`) says which rule fired per emission.

**LANDED as item 440 §(b), with one change: the seam gates EVERYTHING.** The
draft above made a keyed owed emission auto-fire by default and put only the
bare-`idempotent` case behind the knob. Shipped, the knob (`recovery may
re-issue owed emissions [(strength: <level>)]`, item 33) gates the whole seam:
with no policy, `revl recover` auto-fires nothing and the owed-emission report
is item 245's v1 wording byte-for-byte. Auto-firing an emission is a crossing an
operator has to have asked for, and defaulting it on would have been the one
optimistic default this module does not take. The bare rule then admits the
by-construction registers, `(strength: declared)` the author's unverified claim,
and an unregistered owed emission stays human-finish under every setting.

The seam itself is `World.reissue(op)` — a forward re-dispatch of the named call
against the same adapter that already carries `apply_inverse` and
`apply_compensation`. Recover never re-invokes the dead runtime's host body,
which is what the TODO said it lacked; it does not need to, for the same reason
it never needed one to re-issue an inverse.

### 3d. The read tier: the third register (item 440 §(a))

This design's register was two-valued *from recovery's side*: `_roll_back`
branched on `undo_idempotent` and never read `register` at all, so a call that
CHANGES NOTHING was fenced on its first attempt and escalated to an operator on
its second — fail-closed, and therefore safe, but asking a human about calls
that never needed asking.

`undo pure <inverse>(result)` is the third tier, `register: "read"`, ranked
ABOVE `keyed` (a keyed call still crosses and leans on a remote's dedup
contract; a read crosses nothing, so there is no outcome to be ambiguous about).
Recovery re-dispatches it on every run, spends no fence, and never escalates it.

**Negative result, and why the tier is declared rather than derived.** Roadmap
item 440 proposed deriving the tier from the `pure` extern classification revl
already has. That is NOT sound. `pure` is a classification slot, checked for
shape only — its refusal wording ("`pure` means no observable effect") is the
declaration's meaning, never a proof — and the language *requires* a witnessed
inverse to be classified `pure` or `acquire` (rule 3), so the shipped idiom for
a mutating restore is an `extern pure fn` (`extern pure fn close_ledger(h)`,
docs/guide-ai-agents.md). Deriving "changes nothing" from "classified pure"
would therefore promote mutating inverses into the free-replay tier: an
optimistic resolution of an ambiguity, in the unsafe direction. The shipped
design keeps the derivation as a CHECK instead of an inference — `undo pure` is
the author's explicit claim and lowering refuses it unless the named inverse is
itself a `pure`-classified extern — so the claim is anchored to the existing
classification and stays reviewable in the diff.

### 3c. Compensations: the 247 hand-off, closed

A keyed compensation's re-attempt (`recovery.py:566-579`) stays
best-effort and stays a RECORD (247's honesty: compensation is never
inversion), but the residue record upgrades from "landing cannot be
confirmed" to "re-issued under key K; the remote's dedup contract prevents
double-apply". The wording claims the CONTRACT, never the fact: revl
verified the key was stable, not that the remote dedup'd, and the record
carries the retention-window caveat where the audit knows one. The hint at
`recovery.py:579` stops being aspirational. An abort-time
Phase-2 retry after a mid-teardown crash follows the same rule.

## The check and the refusal (question 4)

Static reachability of "a re-run is possible" is simple and honest: any
witnessed/acquire inverse can appear in a crash-replayed teardown once a
WAL is in play, and any deferred emission or compensation can be re-issued
by recovery. So the diagnostic is not path-sensitive cleverness; it is a
completeness sweep over the boundary surface:

1. **Audit fact, always.** `revl audit --recovery` (new view, sibling of
   `--placement`) prints every inverse, deferred emission, and compensation
   with its replay class: `replay: free` (declared/keyed idempotent),
   `replay: fenced` (inverse, undeclared), `recovery: human-finish`
   (owed emission, unkeyed). `audit --diff` flags a transition OUT of
   `free` (a dropped `idempotent` is a reliability weakening, reviewable
   like a new crossing).
2. **Warning under `--record`/`--wal`.** Compiling/running with WAL
   recording enabled, each `fenced`/`human-finish` entry gets a one-line
   diagnostic naming the extern and the consequence ("on a crash-retried
   recovery this inverse will be attempted once and then deferred to a
   human; declare `undo idempotent` or add an idempotency key"). Warning,
   not refusal: the fenced path is SAFE (that is its point), just less
   automatic, and a refusal would break every existing recorded corpus
   program overnight.
3. **Policy-gated refusal, with a STRENGTH argument.** An item-33-style
   policy rule (`requires idempotent-teardown`) lets an operator refuse
   admission of a composition whose recovery surface contains any
   `fenced`/`human-finish` entry, for deployments where unattended
   recovery is a requirement. The bare rule alone is too coarse: it
   cannot distinguish `declared` (trust-me) from `keyed` or `shape-proven`
   (by-construction or statically checked), and the unattended-recovery
   operator's real sentence is "auto-replay only what is keyed or
   shape-proven". So the rule takes a strength argument,
   `requires idempotent-teardown(strength: keyed)`, refusing any inverse
   or emission whose register is weaker than the named floor (order:
   `declared < keyed`, `declared < shape-proven`; the two strong forms are
   peers and either satisfies a strong floor). The bare form means
   `strength: declared` (any register counts), today's behavior. The
   audit already prints the three registers per entry, so the data the
   rule keys on exists on day one. This is where "flagged" becomes
   "refused", by the party who owns the requirement.
4. **Hard refusals (parse/check), day one:** `idempotent(key: p)` where
   `p` is not a declared parameter or not scalar-serializable;
   `idempotent` on a non-emission extern classification slot; `undo
   idempotent` where there is no undo slot (a bare emission);
   a falsified declaration in the sweep (test-time fail, gauntlet-blocking).

## Reconciliations (question 5)

- **G7.** Untouched. G7 remains "LIFO-complete over the accumulated
  effects, once, in-process"; 309 adds the orthogonal re-run axis and
  never reorders anything. The double-undo sweep runs AFTER a G7-shaped
  teardown and re-applies in the same LIFO order (re-run order equals
  first-run order; recovery already does reverse-seq both times).
- **243.** Rule 5's prose requirement becomes declarable and tested; the
  "or replay checkpointed" alternative becomes the fenced path (3a), so
  both of rule 5's arms now exist as mechanism. The stdlib fs inverses
  (Slice 3 / item 244) declare `undo idempotent` and are the first sweep
  passengers; their "idempotent-and-total" prose in Slice 2a point 5
  becomes a checked flag. The witness-is-durable-data rule (243 rule 4) is
  what makes the double-apply test even expressible; 309 depends on it and
  changes nothing about it.
- **247.** An idempotent compensate is safe to RETRY (re-attempt on
  crash-retried recovery and on abort-Phase-2 re-entry); it is still
  best-effort, still audit-surface, still never a proof that the offset
  landed, and a keyed one is additionally safe against the duplicate-land
  ambiguity. 247's open question 3 closes with this doc; its Decision 5
  residue voice is reused unchanged.
- **294.** The key is a 373-style parameter role, not a capability
  valuation (1c above); no change to `cap_order`, the closed registry, or
  the attenuation fold. Where 294's ceiling parameters ERASE into grant
  counters at mint, 309's key parameter THREADS through to every re-issue;
  the two never meet in a comparison.
- **44.** One word, one meaning, three positions: service-method emission
  (shipped), extern emission (new slot), undo slot (new). The runtime
  retry right (TransientError, budget 3) extends to extern emissions with
  the same rule, and the keyed form strengthens the register from claim to
  construction. `delivery-semantics.md` gains the two new positions.
- **125 / fault sweep.** The double-undo round joins the sweep family and
  the cross-tier discipline applies: a descriptor set idempotent on py and
  divergent on go is a portability fail, same as a residue disagreement.
- **118 (folded distributed correlation).** The idempotency key is the
  natural correlation id for distributed rollback; when 118's remote
  deployment lands, the same key field in the WAL descriptor is what a
  remote node dedups and correlates on. Noted as convergence, not designed
  here.
- **322/413.** The fence record (3a) is a new WAL record kind; it inherits
  322's per-tier durability discipline and lands inside 413's integrity
  envelope (a forged fence could suppress a needed replay or force a
  double one, so the fence is exactly the class of record 413's hash
  chain exists for; 413 should land first or together).

## Staged plan (question 6)

Each slice lands green alone; later slices need earlier ones.

- **Slice 1: surface + IR + audit carry.** Parser: `idempotent` in the
  extern modifier slot (emission-only, combinable with
  `async`/`deferred`), the `idempotent(key: p)` role bracket (parse +
  check against the parameter list, scalar type rule), the existing
  `idempotent` kw token accepted in the undo slot. Lower:
  `undo_idempotent` / `idempotent` /
  `idempotency_key` onto the extern IR node and into the WAL
  discharge-descriptor and boundary-descriptor shapes. Audit:
  `revl audit --recovery` view + `--diff` weakening flag. Hard refusals
  (question 4, point 4). Byte-identity for programs that use none of it;
  service-method `idempotent` lowering unchanged.
- **Slice 2: the sweep.** Value-digest trace lines + digest-folding
  fingerprint (the instruments are value-blind today and must not stay
  so); the value-modeling world extension; roundtrip double-undo round +
  dossier facet + registry evidence carry; the recovery-twice unit with
  crash-after-k cut points; key-stability assertion + collision twin;
  declared-but-falsified = fail. py reference tier first, cross-tier via
  the 125 harness (loud-skip where the tier lacks the recording channel,
  the existing discipline).
- **Slice 3: recovery consumes the property.** Free-replay vs
  WAL-fenced-single-attempt in `_roll_back`; the `replay-attempted`
  record (with 413 alignment) on BOTH apply paths, including the abort
  Phase-1 fence before each undeclared apply (option (a), section 3a);
  roll-forward auto-fire of keyed owed emissions; keyed compensation
  re-attempt records; residue-proof and window-proof language for every
  new state. The `--record` warning (question 4, point 2).
- **Slice 4: first-party adoption + policy.** `stdlib/fs.rvl` inverses
  declare `undo idempotent` (and the shape check for native
  restore-to-recorded-value bodies where 244's revl-expressed bodies
  exist); the item-33 `requires idempotent-teardown` policy rule with
  its strength argument; docs
  (`delivery-semantics.md`, `crash-recovery.md` §5/§6 updates).

## Exit tests

Surface and IR:
- `undo idempotent restore(result)` parses on `witnessed` and `acquire`
  externs; `extern emission[x] idempotent fn` and
  `idempotent(key: p)` parse; `idempotent(key: nope)` (undeclared
  param), `idempotent(key: p)` with `p: Pool` (non-scalar), and
  `idempotent` on a `pure`/`acquire`/`witnessed` classification slot all
  refuse with named messages. (`idempotent` is already a reserved
  keyword, item 44, so there is no identifier-compatibility test to run;
  the new slots accept the existing kw token.)
- Byte-identity: the full existing corpus (no new modifiers) admits and
  emits identically; goldens unchanged.

The property test (the item's own equation):
- Undo-twice same state PASSES: a component with declared-idempotent
  inverses passes the double-undo roundtrip round
  (`final2 == final`), and the dossier's `doubleUndo` facet reads pass.
- A NON-idempotent inverse run twice DIVERGES and is caught, BY THE
  VALUE FINGERPRINT: a fixture whose inverse is delta-shaped (counter
  increment) runs against the value-modeling world; the second
  application moves the counter's value digest, so `final2 != final`
  over digests. When declared idempotent this is a hard fail (falsified
  claim); when undeclared it is reported `replay: fenced`
  (informational). The test's negative control is the reason the
  extension exists: the same fixture against the referent-set
  fingerprint PASSES, so the round must assert on digests. Scope stated
  honestly: this catches referent-cardinality and value divergence in
  the model; external-emission divergence is out of the sweep's reach
  and is covered by the mock-remote key tests only.
- Recovery-twice: `recover` run twice on the same mid-abort WAL yields
  an identical world and identical residue proof when all inverses are
  declared idempotent; and for each crash-after-k cut point (first pass
  cut after k of n inverses, second pass complete), the world converges
  to the once-through result.

The key:
- An external effect with an idempotency key DEDUPS: a mock remote that
  counts distinct keys sees ONE application across runtime retry
  (TransientError x3) and across a recovery re-issue; the WAL
  descriptors on both issues carry the identical key value.
- The same mock WITHOUT the key sees two applications on a forced
  re-issue, and the audit surface had said `recovery: human-finish` for
  exactly that extern (the honest-negative twin).
- The COLLISION twin: two DISTINCT logical operations issued under one
  key value (the `idempotency_key=order.id` mistake, one order holding
  several reservations), and the mock remote sees ONE application; the
  test asserts the second operation was silently dropped (one
  reservation leaked) while every retry-safety report read clean. The
  docs name UNDER-apply as the key's failure mode, and open question 3's
  lint targets exactly this shape.

Recovery policy:
- Recovery AUTO-REPLAYS a declared-idempotent inverse on a second
  recovery run (no fence record written, replay happens, world stable).
- Recovery DEFERS a non-idempotent one: first run writes
  `replay-attempted` before applying (crash between fence and apply
  leaves a fence and no double-apply; the report reads
  `fenced-before-attempt, outcome unknown`, never "attempted once");
  second run does not re-apply, reports
  `fenced-before-attempt ... will not re-run` with the referent, exits
  1 on the honest residue.
- The ABORT-THEN-CRASH twin (the headline scenario): an in-process abort
  applies an undeclared inverse in Phase 1 (the fence record written and
  fsync'd first), the process dies before discharge; `revl recover`
  finds the seq undischarged AND fenced, does NOT re-apply, and reports
  the fenced residue. Without the abort-path fence this test
  double-applies; it is the test that makes option (a) load-bearing.
- Roll-forward auto-fires an owed KEYED emission and reports it in the
  window proof; an unkeyed owed emission stays human-finish (existing
  `recovery.py:357` test extended, not replaced).
- A committed transaction is still never rolled back (existing
  discharge-skip tests untouched).
- Keyed compensation re-attempt residue record names the key and claims
  the CONTRACT ("the remote's dedup contract prevents double-apply"),
  never the fact of a dedup; unkeyed keeps today's wording.

Policy and audit:
- `requires idempotent-teardown` refuses a composition with a `fenced`
  inverse and admits it once the declaration (or key) is added.
- The strength floor works: `requires idempotent-teardown(strength:
  keyed)` refuses a composition whose only claim is bare `declared`
  (trust-me) and admits it when the inverse is keyed or shape-proven;
  the bare rule (no argument) admits all three registers.
- `audit --diff` flags removing `idempotent` from an undo as a
  weakening.

## Scoped out (and to whom)

- Proving a host body idempotent: never claimed (G8). The restricted
  native shape check covers only revl-expressed restore-to-value bodies
  (with 244).
- Remote dedup enforcement: the remote's contract; revl guarantees key
  stability and says so (`idempotent: keyed`, not `enforced`).
- Distributed correlation/rollback across nodes: item 118's fold; the
  key field is designed to be reused there.
- WAL integrity of the fence record: item 413.
- Item 37 recorded-property tests as a probe of bare-`idempotent`
  emission claims: that item's scope; 309's sweep covers inverses and
  keys.

## Open questions

1. **Fence granularity.** One `replay-attempted` record per inverse (per
   seq) is designed here; per-recovery-run batching would be fewer
   fsyncs but turns "at most once per inverse" into "at most once per
   run set", which is weaker under partial-batch crashes. Slice 3
   implementer decides with a measurement; the per-seq form is the
   default.
2. **Should `undo idempotent` become the DEFAULT expectation for new
   witnessed externs** (warn on absence even without `--wal`), once the
   stdlib has adopted it? Deferred until Slice 4 shows the adoption
   cost; the fenced path keeps undeclared inverses safe meanwhile.
3. **Key uniqueness discipline.** The checker verifies the key parameter
   exists and threads; it cannot verify the AUTHOR passes a value that
   is actually unique per logical operation. The failure mode has a
   name, silent UNDER-apply: a shared value (a constant, or one order id
   covering several reservations) makes the remote dedup distinct
   operations into one, one of them leaks, and every report reads clean
   ("cannot double-apply" is true and beside the point). A lint (same
   literal in every call site) is cheap; anything stronger is host
   semantics. Slice 2 may add the lint, and the collision-twin exit test
   pins the behavior either way.
