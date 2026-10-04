# Crash recovery

*The accumulator as a write-ahead log — and an honest account of what actually
survives a `kill -9`.*

Implementation: `backends/python/replay.py` (`WriteAheadLog`, the descriptor
and boundary classifiers), `src/revl/recovery.py` (the recovery engine and
verdict), `src/revl/run.py` (`--wal` wiring), `src/revl/mcp/persist.py`
(`resume`, roll-forward's other half), `src/revl/__main__.py` (`revl recover`),
`tests/test_crash_recovery.py`.

---

## 1. Why this exists here and nowhere else

Item 15 (`docs/persistence.md`) makes the *shape* of an admitted composition
durable: snapshot the sources, re-admit them through the gate on restart. What
it does not cover is the effects a half-run activation already committed when
the process died. Those live in the runtime's **effect accumulator** — held in
process memory, paired with the inverses that would undo them — and
`--record` is only an in-memory observer of it, not a durable trace. So a
`kill -9` mid-activation orphans whatever external state the activation already
touched, with no record that it happened.

But look at what the accumulator *is*: an ordered list of committed effects,
each paired with the inverse that undoes it, with the non-invertible ones
(emissions) explicitly marked. That is a **write-ahead log**. A database's WAL
records "I am about to do X, and here is how to undo it" *before* doing X, so a
crash can be recovered by replaying or reversing the log. The paradigm already
computes both halves of every WAL entry as a matter of course. This feature
persists the accumulator as the log it already is, and adds the restart-time
reader that proves a way back.

---

## 2. The honest analysis is the design

The tempting picture is: "persist every effect and its inverse; on restart, run
the inverses and you are back where you started." That picture is wrong, and
being precise about *why* is the whole design.

**After a crash, the process memory is gone.** An inverse that closes over an
in-process object — a local `Map` handle, the fiber's provision registry, a
pooled connection — has nothing left to act on. Running `store.drop()` against a
`store` that no longer exists is a no-op at best. So the accumulator's in-process
inverses are **moot** post-crash: not residue, not a problem, just nothing to do.

What is *not* moot is **boundary state**: the crossings whose referent outlives
the process. There are exactly two kinds, and the frontend already classifies
both:

| boundary | classification (frontend) | after a crash |
|----------|---------------------------|---------------|
| an **emission** — a row written, a message sent | `emission fn` (G4: inverse-or-emit) | already left the process. Bare → still out in the world. Compensated (A5) → the compensation is the cargo. |
| an **acquire** whose resource outlives the process — a file on disk | `extern acquire fn … undo …`, `returns` a resource type | the referent persists; its `undo` is what recovery must run. |
| an **acquire** whose resource dies with the process — a socket | same, but the handle is process-local | moot: the socket died when the process did. |

The `extern acquire` classification, and the resource type it `returns`, is what
lets recovery tell a File from a Socket. `backends/python/replay.py`'s
`_OUTLIVES_HINTS` is the *stated* policy that maps a resource type name to a
lifetime verdict — the language cannot know this in general (a `Handle` could be
either), so the policy is explicit, host-owned, and defaults to "unknown,
therefore prove it" rather than guessing.

So the WAL's real cargo is boundary state, and recovery's real job is to run the
boundary inverses that matter — not to pretend the whole accumulator is
replayable.

---

## 3. The one language-level requirement: reconstructible-from-description

This forces a single point worth stating plainly.

> An inverse that must run *after* a restart has to be reconstructible **from a
> description** — a named boundary operation plus its captured concrete
> arguments — and **not** from a closure.

A closure captures process memory. After a restart that memory is gone, so the
closure cannot run — no amount of persisting it changes that. What *can* survive
is a **description**: "call `fs.unlink('/var/db/PeerWall/gen7.scratch')`". A
fresh process can reconstruct that call and issue it against the world.

The recorder already distinguishes the two. It captures concrete arguments for
exactly one thing — an **emission**, recorded through the service proxy as
`key.method(args)`. Every other inverse the accumulator holds — a compensation
lambda, a provision withdrawal (runtime-derived, R5), an author `undo` over a
local handle — is a **closure**: the recorder has its source site but not a
re-issuable call. `inverse_descriptor()` marks each record accordingly:

```jsonc
// reconstructible: a named call with captured args, re-issuable anywhere
{"reconstructible": true,
 "op": {"receiver": "fs", "method": "unlink", "args": ["/var/db/PeerWall/gen7.scratch"]}}

// closure-only: honest about what it cannot do
{"reconstructible": false,
 "reason": "closure over in-process memory — the recorder holds this inverse's
            source site but not a re-issuable call with captured arguments, so a
            fresh process cannot run it",
 "site": "<gen1>:12", "source": "yield lambda: store.drop()"}
```

A boundary effect that *knows* its own inverse call — a durable resource with a
named `undo` — is appended with an explicit descriptor via
`WriteAheadLog.record_boundary(...)`; that is the reconstructible path a file or
a row takes. Where an inverse is only a closure, the WAL says so. It never
pretends a dead lambda can be re-run.

---

## 4. The WAL format

JSON Lines, append-only, `flush`+`fsync` per record (so a record a caller saw
acknowledged is on disk before the effect it describes is allowed to matter —
the write-ahead discipline). Three record shapes:

```jsonc
// 1. header (first line). `composition` is the digest of the IR the log was
//    opened with (issue #1477); `revl recover --composition` checks it.
{"record": "header", "walVersion": 1, "generation": 7, "guarantee": "…",
 "composition": "sha256:…"}

// 1b. every LATER opening of the same file (a `--watch` reload, or a later
//     run reusing it, #641/#642) records its own composition and the first
//     seq it owns, when it was opened with an IR
{"record": "generation", "generation": 8, "fromSeq": 42, "composition": "sha256:…"}

// 2. one per committed effect, written as it commits
{"record": "effect", "seq": 3, "component": "UserCache", "stepIndex": 4,
 "kind": "emission", "label": "db.execute", "site": "<gen1>:31", "source": "…",
 "origin": {"phase": "call", "key": "cache", "method": "put", "args": ["k","v"]},
 "boundary": {"class": "emission", "referent": "process-crossing",
              "compensated": false, "detail": {"key":"db","method":"execute","args":["…"]}},
 "inverse": {"reconstructible": false, "reason": "an emission is a one-way crossing…"}}

// 2b. an effect a provider recorded WHILE answering another component's
//     required-service crossing carries `within`, that crossing's record. A
//     caller's `emit tickets.file("T1") compensate tickets.withdraw("T1")` and
//     the `file_host` emission Desk's method makes to answer it are ONE
//     crossing in the world: recover reports the nested emission under
//     `nested` ("made inside tickets.file (seq 2); counted there") and counts
//     it with its enclosing record, which is offset, settled or residue as
//     that crossing is. Absent otherwise. Across a placement seam (issue
//     #1889) the bridge carries the caller's crossing with the call, so the
//     provider's records name it with the caller's PROCESS too, and placement
//     recover counts such a record with that crossing when the caller
//     process's WAL holds it ("made inside tickets.file (seq 4 in process
//     agent)"). A caller WAL that is missing, or that does not hold the seq,
//     leaves the record as residue: a fold is never assumed.
{"record": "effect", "seq": 3, "component": "Desk", "kind": "emission",
 "label": "file_host", "within": {"seq": 2, "component": "Agent", "label": "tickets.file"}, …}
{"record": "effect", "seq": 6, "component": "Desk", "kind": "emission",
 "label": "file_host", "within": {"seq": 4, "component": "Agent",
                                  "label": "tickets.file", "process": "agent"}, …}

// 3. activation marker — present iff activation finished cleanly. NOT the end
//    of the log: the WAL stays open for the whole run, so steady-state effects
//    (below) are appended AFTER this line.
{"record": "activation-complete", "generation": 7, "components": ["PgDatabase","UserCache"]}

// 3b. shutdown marker — written at ORDERLY teardown, so its ABSENCE tells a
//     steady-state `kill -9` from a clean exit (issue #536). A crash never
//     reaches teardown, so it never writes this.
{"record": "run-complete", "generation": 7}

// 4. discharge-descriptor — a witnessed (`transactional`) inverse or a
//    `compensation`, as a re-issuable NAMED CALL (items 243 / 247)
{"record": "discharge-descriptor", "seq": 5, "entry": "transactional",
 "call": {"receiver": "db", "method": "delete", "args": ["row#1"]},
 "origin": {"key": "db", "method": "insert", "args": ["row#1"], "site": "svc.rvl:9"},
 "witness": {"row": "row#1"}, "idempotency": null}

// 5. discharge record — the commit-path proof, durable before success is
//    reported; recover SKIPS every seq named here (a committed transaction is
//    never rolled back)
{"record": "discharge", "discharged": [5]}

// 6. model-decision — one model completion made durable at its crossing
//    (item 250 Slice 3a), keyed on the completion's own effect record; the
//    trace hop's payload (item 121), each field tagged with its provenance.
//    No prompt/response text, no digest, no seq. Present only for a crossing
//    that carried a completion; recover ignores it (a fact, not an effect),
//    `revl compare` lists it per side.
{"record": "model-decision", "component": "AgentLoop", "stepIndex": 4,
 "outcome": "validated",
 "llm": {"model": "openai:gpt-4o", "modelProvenance": "host-reported",
         "tokensIn": 1204, "tokensOut": 88, "usageProvenance": "host-reported",
         "latencySeconds": 1.84, "latencyProvenance": "revl-measured-bracket",
         "attempts": 1, "attemptCeiling": 3, "attemptsProvenance": "revl-controlled",
         "verifiedBy": []}}
```

Two optional fields on the descriptors carry the **re-dispatch register** — the
question recovery actually asks about a journalled call: *may I issue this
again?* (items 309 and 440):

```jsonc
// a witnessed inverse the author declared `undo pure` — the READ tier
{"record": "discharge-descriptor", "seq": 5, "entry": "transactional",
 "call": {"receiver": "db", "method": "probe", "args": ["row#1"]},
 "register": "read"}

// a deferred emission the author declared `idempotent(key: id)` — the KEYED
// tier, with the key VALUE captured at enqueue (item 440)
{"record": "deferred-emission", "seq": 7,
 "call": {"receiver": "ledger", "method": "post", "args": ["k1"]},
 "idempotency": "k1", "register": "keyed"}
```

## 4b. The three call tiers (items 309, 440)

| register | what makes a re-issue safe | recovery does |
|---|---|---|
| `read` (`undo pure`) | the call changes nothing, so there is no outcome to be ambiguous about | re-dispatches on every run, spends no fence, never asks an operator |
| `keyed` (`idempotent(key: p)`) | the remote dedups on a stable key carried in the descriptor | re-dispatches freely; a duplicate is the remote's dedup CONTRACT, never a confirmed fact |
| `declared` (`undo idempotent`, bare `idempotent`) | the author's claim, machine-checked for shape only | replays an inverse freely; re-issues an owed emission only under the operator's explicit knob, once, fenced |
| absent | nothing is known | one fenced at-most-once attempt, then `outcome: "unknown"` for a human |

The register set is exactly these three. A fourth name, `shape-proven`, used to
sit beside `keyed` in this table for a check that was never built; item 207
removed it, and `requires register shape-proven` is now a parse error naming
`strong`. It was proposed as a syntactic check over a restore-to-recorded-value
inverse BODY (design 309 §2), and revl has no such body to read: every extern
carries a `@backend` host body, so an inverse is G8-opaque by construction, and
`stdlib/fs.rvl`'s inverses are `@py`. It could also never have been graded by
anything that read the table: its provenance is an INVERSE, and every set that
accepted it — `REDISPATCH_FREE`, the audit's `owed-emission` branch, the Temporal
backend's retry classes — grades a forward deferred EMISSION, which cannot carry
an inverse register. `docs/design/207-checkable-extern-body.md` has the walk.

That leaves a real gap, and §4d says how to write around it: a local MUTATING
inverse cannot reach a strong register, because `keyed` is emission-only and
`read` requires the inverse to change nothing.

The `read` tier is DECLARED, not derived. revl's `pure` extern *classification*
means "not acquire/emission/witnessed" and is checked for shape only, and shipped
examples classify mutating host bodies `pure` (`extern pure fn close_ledger(h)`),
so reading the tier off the classification alone would resolve an ambiguity
optimistically — the one direction recovery never takes. `undo pure` is the
author's explicit claim, and lowering refuses it unless the named inverse is
itself a `pure`-classified extern.

## 4c. The re-issue seam (item 440)

`recover` never re-enters the dead runtime; it re-issues NAMED CALLS against a
`World` adapter. Item 309 §3b wanted the same thing for an OWED deferred
emission — one the commit approved but whose `flushed` record never landed — and
could not, because there was no seam. There is one now: `World.reissue(op)`,
alongside `apply_inverse` and `apply_compensation`.

It is **off by default**. Nothing auto-fires unless an operator turns it on in
the item-33 boundary policy:

```
recovery may re-issue owed emissions
recovery may re-issue owed emissions (strength: declared)
```

The bare rule admits only the by-construction registers (`read`, `keyed`);
`(strength: declared)` additionally accepts the author's unverified claim. An owed emission with **no** register is never auto-fired under
any setting, because whether its pre-crash flush landed cannot be decided. A
`declared` re-issue is fenced before it fires (consume-before-fire, exactly like
item 309 §3a's `replay-fence`), so a crash between the fence and the fire leaves
`fenced-before-attempt, outcome unknown` for a human rather than a second
unprovable attempt. Pass the policy with `revl recover --wal FILE --policy P`.

The `activation-complete` marker's **presence or absence is the entire
roll-forward/roll-back decision**. A genuine `kill -9` can leave a half-written
final line; `WriteAheadLog.read` tolerates it, reports `torn: true`, and recovers
anyway — handling that crash is the whole point.

Record shapes 4 and 5 carry the witnessed-effects teardown across a crash. On
roll-back, recover runs the boundary inverses in two phases: transactional
inverses reverse-seq (skipping any seq with a durable `discharge` record — a
COMMITTED mutation is retained, not rolled back), then owed compensations
best-effort as a further crossing that records, never clears, its referent (so a
re-issued compensation is honest RESIDUE, never falsely CLEAN). The descriptor
schema, the discharged-seq skip, and the merged residue records are specified in
`docs/design/teardown-contract.md` (WAL descriptor + the owned py-tier migration).

Wire it up with `revl run … --wal FILE` (implies `--record`). The log is opened
before activation and **stays open for the whole run** (issue #536): each effect
is appended as it commits — during activation AND in steady state after it — a
clean activation stamps `activation-complete`, and an orderly teardown stamps
`run-complete` and closes the log. Closing at `activation-complete` (the old
behaviour) made every steady-state crossing after activation invisible, so a
`kill -9` in steady state read as `ROLLED-FORWARD [CLEAN]` while the crossings
sat unrecorded — the log now carries them, and their `run-complete`-less tail is
what recovery reads as a steady-state crash.

---

## 4d. "Verified, not merely claimed", for a local mutating inverse

An operator who writes `requires idempotent-teardown(strength: strong)` means
"auto-replay only what revl checked, not what the author asserted". A local
mutating inverse cannot satisfy that floor and never will: `keyed` is
emission-only, and `read` requires the inverse to change nothing, so
`stdlib/fs.rvl`'s `restore`, `unrm`, `unmove` and `rmdir_if_empty` are
permanently `declared`.

The sentence is still writable, and the rule that writes it produces STRONGER
evidence than a body-shape register could. Pair the register floor with an
evidence floor:

```
capability fs requires register declared
capability fs requires evidence [inverse-roundtrip pass, attestation valid]
```

The register says the claim was MADE. `inverse-roundtrip` says it was TESTED:
item 309's value-aware fault sweep runs the double-undo against the real `@py`
body and diverges on the second undo when the inverse is delta-shaped or
refund-shaped rather than restore-to-recorded-value, which catches a lying `undo
idempotent`. `attestation valid` roots the dossier, so the evidence is the
operator's fact rather than the publisher's (290 §6.2). A syntactic rule over a
declared body shape could not do this: it grades declared leaf algebras rather
than behaviour, and its own "no reads of current state" condition rejects all
four fs inverses, each of which branches on `lexists_confined`.

## 5. Roll forward vs roll back

`revl recover --wal FILE` reads the log and decides:

### Roll forward — activation completed before the crash

The marker is present, so the crash happened *after* activation finished. There
is no in-flight boundary state outstanding, and the composition's shape is
durable via item 15. Recovery **resumes the persisted generation** through
`persist.resume` (which calls item 15's `restore` — replaying admission through
the *current* gate, so a generation the current checker now rejects fails loudly
rather than resuming on stale authority). Pass `--restore SNAPSHOT.json` to
supply the generation to resume.

```
verdict: ROLLED-FORWARD
world: MODEL. Recovery ran against an in-memory model; every call below was modelled, not performed. ...
  committed effects (all balanced): 6
  components: PgDatabase, UserCache
residue proof [CLEAN] in the model:
  world: model. ... a completed activation left the accumulator balanced;
  there is nothing half-done to roll back.
```

This `[CLEAN]` verdict holds when the log also carries `run-complete` (an
orderly shutdown) **or** the crash landed before any steady-state crossing.

#### Roll forward, but crashed in steady state (issue #536)

`activation-complete` is present but `run-complete` is not, and the log carries
effects appended *after* the activation marker: the process was `kill -9`'d in
steady state. The composition's shape is still durable via item 15, so the
verdict stays `ROLLED-FORWARD` — but the post-activation crossings are in-flight,
not accounted, so recovery surfaces them as residue rather than claiming CLEAN.
A durable crossing (an emission that left the process, an acquire whose resource
outlives it) is **still out in the world**; an in-process crossing is **moot**.

```
verdict: ROLLED-FORWARD
world: MODEL. ...
  committed effects (all balanced): 4
  components: UserCache
  RESIDUE  db.execute             steady-state crossing — still out …
residue proof [RESIDUE] in the model:
  RESIDUE: 1 boundary crossing(s) committed AFTER activation-complete with no
  `run-complete` shutdown marker — the run was `kill -9`'d in steady state …
```

`revl recover` exits `1` here, as for any honest residue (section 5b). Recovery does not
auto-reverse a steady-state crossing (it was committed, intended work, not a
half-run activation); it reports it so an operator reconciles it or resumes the
generation.

### Roll back — the process died mid-activation

No marker. Recovery reconstructs the boundary inverses from their descriptors
and runs them **newest-first (LIFO)** — exactly the order an L-Raise teardown
(`docs/fault-tests.md`, G7) unwinds the accumulator. Each record lands in one of
three lanes:

- **ran** — a reconstructible boundary inverse; its `op` is re-issued against the
  world (a file unlinked, a row deleted).
- **moot** — an in-process referent; its memory died with the process, so its
  inverse is a no-op. Reported, but not a problem.
- **unreconstructible** — a boundary inverse the recorder held only as a closure,
  or a bare emission with no inverse at all. It *cannot* run. Reported as
  **residue**: the durable state is still out in the world.

```
verdict: ROLLED-BACK
world: MODEL. Recovery ran against an in-memory model; every call below was modelled, not performed. The outside world was not touched.
  ran      create scratch         fs.unlink('/var/db/PeerWall/gen7.scratch') [modelled, not performed]
  moot     provide cache          in-process (memory gone)
  RESIDUE  db.execute             closure-only — still out: db:execute:(…)
residue proof [RESIDUE] in the model:
  world: model. Every inverse, compensation, re-issue and reclaim counted here
  was modelled, not performed: ... RESIDUE: 1 boundary inverse(s) were closure-only and could not be
  reconstructed, so 1 durable referent(s) are still out in the world (…).
  Reported honestly — the WAL never claimed a dead closure ran. Declare a
  reconstructible inverse (an `extern acquire … undo …` / an emission
  `compensate`) for these to make them recoverable.
```

The verdict is **checked**, not asserted: recovery seeds a `World` (a `DictWorld`
by default; a real host supplies an adapter over the actual filesystem/database)
with every durable referent the WAL says was created, runs the reconstructible
inverses against it, and the **residue proof** is the set of referents still
present afterward. Clean iff that set is empty. The `World` is the catch:
without `--composition`, `revl recover` runs against the model, and the proof
is a proof about the model. Section 5b says what that means for the output and
the exit status, and section 5c how to recover against the real world.

### 5b. The model is not the world (issue #1477)

Every inverse, compensation, re-issue and shared reclaim recover performs goes
through a `World` adapter. The adapter declares what it is with `kind`:
`"model"` for an in-memory stand-in, `"real"` for an adapter over the actual
outside world. The default is `"model"`, and `DictWorld` is one.

Without `--composition` (section 5c), the CLI runs against `DictWorld`.
Nothing it reports as ran, rolled back, re-attempted, re-issued or reclaimed
happened to a file, a row or a remote service. The output says so:

- the verdict JSON carries `"world": "model"` (or `"real"` for an adapter that
  declares it), on every verdict, including roll-forward and fork-retired,
  and `"worldCalls"`: how many calls recover made against that world. For a
  model it is how many calls the model stood in for;
- the rendered verdict's second line is `world: MODEL. ...`, and every line
  that reports a call against the world ends in `[modelled, not performed]`;
- the residue proof starts with `world: model.` and its header reads
  `residue proof [CLEAN] in the model:` (or `[RESIDUE] in the model:`).

Exit status:

| exit | meaning |
|---|---|
| `0` | clean, and the world was real, or the model stood in for no call (`worldCalls: 0`), or `--model-only` accepted the model |
| `1` | honest residue remains (in the model it is still residue: the model could not clear it either) |
| `3` | clean in the model, the model stood in for at least one call, and `--model-only` was not given. Nothing out there was reconciled |

A clean roll-forward, or a roll-back whose every effect was moot, makes no call
against any world. Its exit `0` is as true of the outside world as of the
model, so it needs no `--model-only`; its JSON still says `"world": "model"`.

A model run that stood in for a call and has no `--model-only` also prints
`revl recover: not reconciled` on stderr. Pass `--model-only` when a model run
is what you want, for example to read what recovery would do before doing it:
the output is the same, still marked as modelled, and the exit status then
follows the modelled residue.

**A model run never spends a fence.** The at-most-once fences
(`replay-fence`, `reissue-fence`, `shared-reclaim-fence`) say "an attempt
against the outside world was about to start", so recover writes them only
against a real world. A model run attempts nothing out there; if it wrote a
fence, the real recovery after it would find the fence and refuse an inverse
that never ran anywhere. So any number of model runs leave the WAL's fences as
they found them. What a model run still writes is world-independent: the
roll-forward window's `discharge` record and a finalized two-phase admission's
records, as before.

### 5c. Recovering against the real world (issue #1477)

```
revl recover --wal run.wal --composition agent.rvl [--config config.toml]
```

`--composition` names the composition that wrote the WAL, as passed to `revl
run`, and `--config` the config it ran with. Recover then:

1. **Checks the composition against the log, opening by opening.** A WAL
   opened with an IR carries the IR's digest in its header (`composition`, a
   sha256 over the canonical IR with each `.rvl` path reduced to its basename,
   so the working directory does not matter), and every later opening of the
   same file writes a `generation` record with its own digest and the first
   seq it owns. Each open call belongs to the opening that wrote it, and is
   replayed only through that opening's composition. A call another opening
   wrote is `generation-residue`, not attempted, and names the opening
   ("generation 1 (opening 2 of this log, from seq 9)") and its digest: run
   recover again with that composition. When the composition wrote none of the
   open calls, recover refuses outright and names every opening it checked; an
   opening with no digest (written before this change, or without an IR) is
   named as such. Replaying a call through another composition would call the
   wrong host bodies with the right arguments. Two runs of the same
   composition may both say `generation 1`; the opening number tells them
   apart.
2. **Loads the composition's emitted module without activating it**, through
   the driver's own plug seam, so extern config and bound secrets are
   installed and no activation body runs again.
3. **Boots only the providers the open descriptors call through, and only
   when booting them crosses nothing.** A descriptor whose `call.receiver` is
   a required-service key needs the live provider. Recover boots the
   components that provide those keys, and what they require, and names them
   in the verdict (`binding.booted`). Booting a component runs its
   activation, so a provider whose activation crosses the boundary (a
   non-pure extern it reaches, directly or through functions, or an emission
   method of a service it requires; teardown-position crossings count, since
   recover unloads what it boots) would make that crossing a second time,
   and the WAL already holds the first. Recover reads each provider's
   activation crossings off the IR with `revl audit`'s boundary walk, its
   provided methods and compensations left out because booting runs neither,
   and does not boot a provider that has any, or whose required components
   have any. Each such key is listed in `binding.refused` (`key`,
   `component`, `crossings`), and every call through it is declined by name
   (`would-reactivate`). A second recover with nothing open boots nothing.
4. **Replays the open discharge descriptors through the runtime's own abort
   path** (`runtime.replay_descriptors`): witnessed inverses newest first with
   their fences, then compensations newest first under the Phase-2 budget.
   The runtime appends an `aborted` record naming every seq that ran, which
   settles it: a later recover, real or model, finds it settled.

The verdict carries `"world": "real"` and a `binding` object (`composition`,
`digest`, `booted`). Per descriptor:

| runtime outcome | in the verdict |
|---|---|
| `ran` | a transactional inverse in `transactionalRolledBack`; a compensation in `compensationsRan` (performed, its host body returned; not residue) |
| `settled` | `settledByReplay`: an earlier replay already settled it, nothing re-run |
| `failed` | residue (`restore-residue` or `compensation-residue`, outcome `failed`) |
| `fenced` | residue (`fenced-residue`): an earlier attempt spent the at-most-once fence |
| `unresolved` | residue (`unresolved-residue`): the call names no host body in this binding |
| `stranded` | residue (`stranded-residue`): an E-Stop is in force, nothing ran |
| `would-reactivate` | residue (`reactivation-residue`): reaching the receiver needs a provider whose activation crosses (`binding.refused`); not attempted, and no fence is spent |

A descriptor whose arguments were not captured at registration (`args: null`,
a compensation whose argument is itself a call) or were redacted as
`Secret[T]` is never handed to the runtime: recover never guesses an argument.
It is residue, named by its call, for example `a.y(<not captured>)`.

What else the binding does, call family by call family. Each goes to its own
runtime entry point, so the fences and settling records are the runtime's:

- **A legacy boundary inverse** (an `effect` record with a reconstructible
  `op`, written by `record_boundary`) is a named call with captured arguments,
  so it goes to `runtime.replay_descriptors` as the transactional entry it is,
  in the same batch as the descriptors: one seq space, one LIFO order, the
  runtime's fence and `aborted` record. It lands in `ran`. The one py-tier
  writer today is a durable-cursor subscription, whose op is
  `Stream.close(cursor)`; the runtime resolves `Stream` to its own class and
  closes whatever live subscription resumes from that cursor. In a fresh
  process nothing is live, so the close has nothing left to do; the recorded
  position is kept, which is the point of a durable cursor.
- **An owed deferred emission** is re-fired through `runtime.reissue_deferred`,
  the session's own flush: the same E-Stop check before the host body and the
  same `flushed` (or `flush-residue`) record after it. Recover keeps what it
  always owned: the operator's policy (`--policy` with `recovery may re-issue
  owed emissions`), the tier, and the `reissue-fence` it writes before the
  fire. A second recover reads `flushed` and fires nothing.
- **A shared reclaim** runs through `runtime.reclaim_shared`, which writes the
  handle's `shared-reclaim-fence` before the inverse and `shared-complete`
  after it. A second recover finds the completion and reclaims nothing; a
  fence with no completion is an unknown outcome and is not re-fired.

Against the model none of these is performed, and none spends a fence.

A compensated emission is **offset** once its compensation ran. Each
compensation descriptor names the emission it offsets (`offsets`, the seq of
the emission's `effect` record), and after the replay recover re-asks, for
every emission it had counted closure-only residue, whether its compensation
is now settled; if so the emission moves to `offset` and out of the residue. A
bare emission (one with no compensation) still crossed the boundary and stays
residue. So a crash whose every emission was compensated recovers CLEAN, exit
`0`; the exit status follows the residue as always.

### 5d. Recovering a placement run (issue #1477)

```
revl run app.rvl --placement p.toml --wal run.wal
revl recover --wal run.wal --composition app.rvl
```

A placement runs each process on its own runtime, so it writes **one WAL per
process**. With `--wal FILE`, every py process records its own crossings to
`FILE.<process>` (for `run.wal` and processes `desk` and `agent`:
`run.wal.desk`, `run.wal.agent`), through the same recorder a single-process
run uses. FILE itself is the run's **index**, JSON Lines:

- `placement-index` (first line): `placementVersion` and `processes`, each
  with its `name`, `components`, `backend` and `wal` (a file name relative to
  the index, so the set moves as one);
- `opened` (`run`) per run that armed the index;
- `committed` (`run`) once every process of that run was UP.

**The commit is the placement's, not a process's.** A placement's activation
is the whole composition's, so no process stamps its own `activation-complete`
when its components finish. Once every process says UP, the conductor writes
`committed` to the index, then tells each process to stamp its marker. A crash
before that leaves every process WAL uncommitted, and recover rolls all of them
back, as it would the same composition run in one process. A crash between the
index's `committed` and a process's marker is completed by recover: it stamps
that process's `activation-complete` from the index (`commitStamped` in the
verdict), so the process rolls forward with the others instead of back alone.

`revl recover --wal FILE` on an index recovers the whole run:

1. **Finds every process WAL the index names.** A missing one is residue named
   by its process (`missing-wal`): recover cannot tell what it crossed. An
   empty one was created by the conductor and never opened by its process,
   which crossed nothing.
2. **Recovers processes consumers first**: a process that requires a key from
   another is recovered before it, so compensations run newest first across
   the run as within one process.
3. **Replays each WAL through the composition's binding** (section 5c), with
   one emitted module and one boot of the providers the open calls of every
   process go through. A compensation declared in one process through a
   service another process provides reaches the component that provides it;
   the process verdict's `binding.reached` names that component and the
   process the placement hosted it in. A key no process provides is listed in
   `binding.unreached`, and the runtime reports its calls as residue.
4. **Gives one verdict** (`"verdict": "placement"`): `placement` (the index,
   the run, whether it committed, the recovery order), `processes` (each
   process's own verdict), and one `residue` whose `outstanding` entries carry
   their `process` and whose proof names each process's residue. It is clean
   only when every process is clean. A second recover performs nothing.

`revl estop --wal FILE` on an index reads every process WAL it names: the
outstanding entries are listed per process, and the inventory is known only
when every process WAL could be read.

Read as a WAL, an index has no records, so a reader would call a crashed
placement clean. Every WAL reader refuses it instead
(`PlacementIndexNotAWAL`), naming the process WALs.

`--wal` with `--placement` is refused for a process on a tier other than py
(its placement runner writes no WAL) and for a sandboxed process (its WAL
would sit outside the sandbox). `revl swap` is refused while a WAL is armed: a
successor would write a WAL the index does not name. An existing index is
reused only for the same processes and components; recover a different
placement's run first, or name a new path. `--restore` does not apply to a
placement run.

### Recovering a session that was forked (item 250)

Two WALs read differently once a session has been forked.

- The **frozen parent** carries `fork-frozen`. It was retired at the fork step:
  its history above k was rewound into the branch and it takes no further steps,
  so recovery neither rolls it forward nor rolls it back live. The verdict is
  `FORK-RETIRED`, and the residue it reports is the crossed emissions and the
  enumerated-but-unfired crossing inverses that `fork-begin` made durable
  *before* the rewind, so a crash cannot lose them.
- The **branch** carries `fork-branch`. It recovers exactly as any other session
  does, over its own witnessed effects — but the report also names its lineage,
  because a branch's rollback lands at the **fork point**, not at an empty
  workspace. The state below the fork point is the parent's rewound step-k state
  and is not the branch's to restore. Without that line an operator could read a
  branch's clean rollback as "the workspace is back to nothing".

`revl branch --wal FILE` reads the same lineage without recovering anything, and
`revl compare LEFT.wal RIGHT.wal` diffs two histories that share a fork point
(see [commands-reference.md](commands-reference.md)).

---

## 6. What this refuses to promise

- It does **not** promise state was restored. As with backwards replay
  (`docs/replay.md`), running an inverse means the inverse *ran*; whether that
  restored application state is the application's own equivalence.
- It does **not** reconstruct closure-only inverses. It reports them. The remedy
  is to declare the boundary with a reconstructible inverse (`extern acquire …
  undo …`, or an emission `compensate` whose call and arguments are captured), so
  the description survives the process.
- It does **not** undo a bare emission — an emission already crossed the boundary
  and has no inverse by construction (G4). Recovery names it; it does not pretend.
- It works from the durable log alone. Recovery never re-enters the dead runtime,
  which is the point: the process is gone, and the WAL was written so that its
  absence would not matter.
