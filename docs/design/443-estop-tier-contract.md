# 443: the E-Stop tier contract

**Roadmap:** item 443 · **Issue:** #122 · **Reasoning of record:** docs/design/443-estop.md (the py tier and the formal layer) · **Status:** DECISION, 2026-09-08

## What this note adds

443-estop.md decided the verdict and built it on the py reference tier. Since
then the go and rust runners honour the latch (PR #611), the ts tier has the
latch reader and the two bridge seams but no inventory or runner line (#769
open), and a java + wasm attempt (#620) was closed unmerged. `revl.estop.
TIERS_WITH_ESTOP` is `{py, go, rust}`. The remaining work is not more
reasoning about the verdict; it is a contract every runtime implements the same
way, so the conductor's report means the same thing whichever tier a component
lands on. This note fixes that contract, answers the three questions the
maintainer asked in tier-independent terms (the state afterwards, what
"stranded-but-owned" is, how recovery sees it), and records two per-tier
decisions (java honours, wasm is reported statically). It reopens nothing in
443-estop.md.

## The verdict, reconciled with G4, G7 and A8

| guarantee | under `commit` / `abort` | under `halted` |
|---|---|---|
| G4: every mutation carries an inverse | the inverse is registered and, under abort, replayed | UNCHANGED. The inverse is registered; the halt does not run it and does not drop it. G4 is a claim about the program text, and the text did not change |
| G7: derived LIFO teardown, complete over registered entries | replays every entry LIFO | vacuous (the replay set is empty) and still complete: the third column of the disposition table, `RevL.G7.estop_strands_everything`, `halt_books_are_total` |
| A8: a failed activation reverts its accumulated prefix, no residue escapes | the failure route | NOT A FAILURE ROUTE. A8 governs faults; the E-Stop is a verdict an operator chose, not a fault the runtime detected. A8's obligation (nothing escapes unreported) transfers to the recovery path, and the halt's own claim replaces R4: not "no residue" but "all residue, all of it reported" |

The one sentence to keep: **an E-Stop trades resource cleanliness for stop
latency, and pays for it with total accounting.** A program that cannot afford
the trade uses `revl_abort`.

## The state afterwards

A halted process is in a terminal state named `Halted`. It is not `Unloading`
(nothing unwinds) and not `Active` (nothing serves). Concretely, on every tier:

- Every fiber keeps the state it had at the instant of the halt. No transition
  runs. A `Loading` activation stays half-loaded; an `Active` one stays active
  in name only.
- Provisions are neither withdrawn nor served. A call on any provided key
  refuses with the halt's reason (`EstopEngaged` on py; the same error string on
  every tier). A withdrawal would be an inverse, and the halt runs none.
- The management plane refuses every mutating verb (`load`, `swap`, `call`,
  `commit_confirm`, `abort`, `restore`, `fork`); `unload` strands rather than
  unwinds; only the read verbs (`state`, `estop_report`) answer, and `state`
  carries `halted: true` with the halt record.
- The instance never resumes. The paths back are `revl recover --wal` and the
  operator's hands (443-estop.md, open question 3).
- Under placement the process exits non-zero after printing its inventory
  (E7 below). Under an in-process session (MCP) the process stays up for the
  report only.

### Across a process seam: a halt is seen as peer death, not as a halt

A halted provider stops answering its socket. Its consumers in OTHER processes
are not halted; they see provider withdrawal (R2/R3): the proxy's monitor
fires, the consumer deactivates reactively and replays its own inverses LIFO.
This is deliberate and it is the right asymmetry: the emergency is in the
halted process, and the consumer's graceful unwind cannot make the halted
process cross a boundary again. The same holds across the two-composition
topology (item 151) and a network seam (docs/network-path.md): a halt never
propagates as a halt. An operator who wants the whole placement halted arms the
conductor (`revl run --placement P --estop-latch FILE`), which halts every
child by name.

## Stranded-but-owned, defined

An entry is **stranded-but-owned** at the halt exactly when:

1. it was registered on an activation's disposer stack (a `bracket`, a
   `transactional`, a `compensation`) or is the activation's one in-flight
   crossing, and
2. no `discharge` record for it follows its registration in the WAL, and
3. no inverse, compensation or flush was run for it by the halt (the halt runs
   none, so this is always true; it is stated so the definition is checkable).

The **owner** is the activation that registered it, and ownership does not move
at the halt. Each record therefore names `component`, `method`, the WAL `seq`
and the entry `kind`, plus what the tier can still say about the obligation:

| kind | what the halt keeps | what recovery can do with it |
|---|---|---|
| `transactional` (243) | the WAL-logged named-call descriptor and the witness | re-issue the inverse against the witness |
| `compensation` (247) | the WAL-logged descriptor with its captured args | re-issue if keyed (309), else refuse as `unknown` |
| `bracket` (acquire) | the inverse closure, in process memory only, unless the tier writes descriptors for acquisitions | reported as `unreconstructible` residue: the handle is held until the process exits, and recovery says so instead of pretending it ran |
| in-flight crossing | the descriptor, marked dispatched, no completion | `outcome: unknown`; keyed crossings may be re-issued, unkeyed ones are the operator's |

Ownership transfers only when reconciliation runs: `revl recover` writes the
discharge (or the refusal) for each owed descriptor, and that record is what
moves the entry off the books.

The two residue kinds are unchanged from 443-estop.md: `estop-stranded`
(`attemptedFlag: false`, `outcome: "not-attempted"`, `error.type: "estop"`) and
`estop-ambiguous` (`attemptedFlag: true`, `outcome: "unknown"`, at most one per
activation, `RevL.G7.halt_ambiguity_is_at_most_one`). Both carry `state:
"unresolved"`.

## How recovery sees it

An E-Stop is shaped to look like a crash to `revl recover`, on purpose, because
revl already has a proven answer for a crash (docs/crash-recovery.md). The
contract for recovery, tier-independent:

1. The WAL has registration descriptors with no discharge behind them and no
   `activation-complete`. Recovery rolls back: it reconstructs the owed inverses
   and compensations newest-first and reports each as run, refused (`unknown`)
   or unreconstructible.
2. The latch file beside the WAL (`<wal>.estop`) is the durable marker that this
   was a halt and not a crash. Recovery reads it (`src/revl/recovery.py` already
   does) and the report says "halted by <operator>: <reason>", naming the halt
   record's `at`, before the residue lines. A WAL with owed descriptors and no
   latch is a crash; the residue treatment is identical.
3. An `estop-ambiguous` record after a two-phase admission decision (design 460
   §6.2) reclassifies that decision `ambiguous`: recovery neither finalizes nor
   reverts it, and the operator reconciles it exactly as any other ambiguous
   crossing.
4. `revl estop --report --wal FILE` is the read-only view of the same books and
   must agree with what recovery would list; the two are pinned against each
   other by the conformance fixture below.

## The tier contract

A tier is in `TIERS_WITH_ESTOP` when, and only when, its runtime meets E1 to E8
and passes the conformance fixture. A tier that does not is in the SIGKILL
population, reported UNKNOWN by name, which is honest and is the default.

- **E1, one latch reader.** Path resolution (`--latch`, else `<wal>.estop`,
  else `REVL_ESTOP_LATCH`) and the fail-closed rule (absent file: not halted;
  readable but malformed: HALTED; unreadable for any other reason: HALTED) are
  byte-identical to `src/revl/estop.py::read_latch`. go, rust and ts already
  ship this reader; java copies it.
- **E2, every crossing seam consults it.** Before dispatching or accepting a
  new boundary crossing: activation (`plug`), an acquisition, a witnessed
  registration, a compensation registration, a deferred flush, an approval
  crossing, an outgoing seam proxy call, an incoming stub accept, and, on tiers
  with streams (130), `subscribe` and `next`. A seam that is refused records
  nothing and runs nothing.
- **E3, unarmed is free.** No latch, no watcher thread, no file read, every
  existing teardown path byte-identical. The cost while armed is one `open()`
  per crossing.
- **E4, an in-flight registry.** The crossings that are genuinely dispatched
  (an approval crossing, a deferred flush, a teardown inverse, a seam call, a
  stream `next`) are bracketed so the halt can name the at-most-one
  dispatched-and-unconfirmed crossing per activation as `estop-ambiguous`.
- **E5, the inventory line.** On the halt the process prints exactly one line,
  `[<name>] HALTED <json>`, where `<json>` is the halt record of 443-estop.md
  (`reason`, `operator`, `at`, `activations`, `inFlight`, `stranded`,
  `resumable: false`, `reconcile`), with residue records in the merged residue
  schema. The conductor merges it by name; nothing else is parsed.
- **E6, an idle watcher.** Because E2 engages lazily at the next crossing and an
  idle process crosses nothing, an armed process polls the latch on a timer
  and halts from the watcher when it appears.
- **E7, die where it stands.** After E5 the process exits non-zero without
  running teardown, without printing `DOWN`, and within
  `REVL_ESTOP_HALT_WINDOW` (default 2s), which buys the inventory and nothing
  else.
- **E8, no self-halt.** No extern, no stdlib binding, no host-body reach to the
  latch writer. The only writers are `revl estop`, the conductor and the MCP
  `revl_estop` verb behind the `estop` operator authority.

## Per-tier decisions

| tier | decision | what remains |
|---|---|---|
| py | reference, landed | none |
| go, rust | landed (PR #611) | conformance fixture run |
| ts | finish #769: the in-flight registry (E4), the `HALTED` line from `placement_runner.ts` (E5), the idle watcher (E6), then join `TIERS_WITH_ESTOP` | the seams (E1, E2 at the bridge) are landed |
| java | honour, like go and rust: latch reader in `backends/java/placement`, accept seam in the stub, registry, `HALTED` line, watcher in both `PlacementRunner` and `RealPlacementRunner`; the cordis4j reactive runner and the JDK-17 stub print the same line | all of it; #620 is the starting point and is re-split so java lands alone |
| wasm | **does not honour the latch itself, deliberately, and is reported STATICALLY rather than UNKNOWN.** A wasm instance has no process, no clock and no file access; its embedder is the halt authority, and halting it is dropping the instance without running its teardown. Its inventory is knowable from outside: the `revl:teardown` custom section (243 Slice 2b) enumerates every witnessed and compensation descriptor at compile time, so the conductor or embedder prints `[<name>] HALTED <json>` on the instance's behalf with those entries as `estop-stranded (static)`. Ambiguity is the host's to name: a wasm crossing goes through a host import, so the host's in-flight registry (E4) names the one unconfirmed crossing. wasm therefore joins the report with a third population label, `static`, not `TIERS_WITH_ESTOP` | the static inventory projection in the conductor, and the host-import registry on the wasm edge gate (335) |

The `static` population is a strict improvement over UNKNOWN for the one tier
that cannot run a watcher, and it costs no runtime seam. A wasm host that later
exposes a clock and a latch read can move to the honouring population under the
same E1 to E8; nothing here forecloses it.

## Conformance fixture (the exit test for each tier)

One scenario, `tck/estop/`, driven per tier by the placement runner's existing
`--once` harness:

1. An activation registers two brackets, one witnessed (`transactional`) entry
   and one compensation, then dispatches one crossing that blocks on a fixture
   endpoint that never answers.
2. The fixture arms the latch while the crossing is out.
3. Assertions, identical on every honouring tier:
   - zero inverses, compensations or flushes ran (the fixture endpoints count);
   - the process printed exactly one `[<name>] HALTED <json>` line that parses
     into the halt record, with `stranded` naming the witnessed entry and the
     compensation, `inFlight` naming the blocked crossing as `estop-ambiguous`,
     and `activations[].stranded` summing to the registered count (the books
     partition the stack);
   - the process exited non-zero with no `DOWN` line, inside the halt window;
   - `revl recover --wal` lists exactly the owed descriptors (the witnessed
     inverse re-issued against its witness, the compensation refused or
     re-issued per its key, the brackets as unreconstructible, the crossing as
     `unknown`) and names the operator and reason from the latch;
   - `revl estop --report --wal` lists the same books;
   - a consumer of the halted provider in a second (unarmed) process
     deactivated reactively with its own no-residue proof.
4. For wasm, the same fixture with the static projection: the conductor's line
   for the instance lists the `revl:teardown` entries as `estop-stranded
   (static)` and the host registry's crossing as `estop-ambiguous`.

A tier is added to `TIERS_WITH_ESTOP` in the same PR that makes its fixture run
green, never before.

## What stays open

- The bracket descriptor gap: brackets are closure-only on every tier, so
  handles held at a halt are always `unreconstructible` residue. Writing
  acquisition descriptors to the WAL (the item-309 idempotent-inverse register
  already knows which inverses are safe to re-issue) would let recovery release
  them; that is its own item, not part of this contract.
- The remote side of a halt: a halted composition's outstanding remote tasks
  (439) keep running on the peer; the stranded compensation names them so
  recovery can cancel them. Recorded in docs/design/439-a2a-task-lifecycle.md.
