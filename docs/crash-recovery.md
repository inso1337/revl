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
// 1. header (first line)
{"record": "header", "walVersion": 1, "generation": 7, "guarantee": "…"}

// 2. one per committed effect, written as it commits
{"record": "effect", "seq": 3, "component": "UserCache", "stepIndex": 4,
 "kind": "emission", "label": "db.execute", "site": "<gen1>:31", "source": "…",
 "origin": {"phase": "call", "key": "cache", "method": "put", "args": ["k","v"]},
 "boundary": {"class": "emission", "referent": "process-crossing",
              "compensated": false, "detail": {"key":"db","method":"execute","args":["…"]}},
 "inverse": {"reconstructible": false, "reason": "an emission is a one-way crossing…"}}

// 3. terminal marker — present iff activation finished cleanly
{"record": "activation-complete", "generation": 7, "components": ["PgDatabase","UserCache"]}

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
before activation; each effect is appended as it commits; a clean activation
stamps the marker.

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
  committed effects (all balanced): 6
  components: PgDatabase, UserCache
residue proof [CLEAN]:
  a completed activation left the accumulator balanced; there is nothing
  half-done to roll back.
```

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
  ran      create scratch         fs.unlink('/var/db/PeerWall/gen7.scratch')
  moot     provide cache          in-process (memory gone)
  RESIDUE  db.execute             closure-only — still out: db:execute:(…)
residue proof [RESIDUE]:
  RESIDUE: 1 boundary inverse(s) were closure-only and could not be
  reconstructed, so 1 durable referent(s) are still out in the world (…).
  Reported honestly — the WAL never claimed a dead closure ran. Declare a
  reconstructible inverse (an `extern acquire … undo …` / an emission
  `compensate`) for these to make them recoverable.
```

The verdict is **checked**, not asserted: recovery seeds a `World` (a `DictWorld`
by default; a real host supplies an adapter over the actual filesystem/database)
with every durable referent the WAL says was created, runs the reconstructible
inverses against it, and the **residue proof** is the set of referents still
present afterward. Clean iff that set is empty. `revl recover` exits `0` on a
clean recovery, `1` when honest residue remains.

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
