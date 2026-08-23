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
```

The `activation-complete` marker's **presence or absence is the entire
roll-forward/roll-back decision**. A genuine `kill -9` can leave a half-written
final line; `WriteAheadLog.read` tolerates it, reports `torn: true`, and recovers
anyway — handling that crash is the whole point.

Wire it up with `revl run … --wal FILE` (implies `--record`). The log is opened
before activation; each effect is appended as it commits; a clean activation
stamps the marker.

---

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
