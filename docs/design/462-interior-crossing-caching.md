# 462 (provisional): interior-crossing extern caching

**Provisional roadmap id.** This note is filed against GitHub issue
[#97](https://github.com/inso1337/revl/issues/97) (roadmap item 310,
capability-aware caching) and specifies its last remaining slice: design
slice 4, `cache` on an extern declaration whose crossing happens INSIDE a
body rather than at the seam. The number 462 is a placeholder chosen so the
file sorts after the last numbered design note; the orchestrator assigns the
real item number at merge and renames this file. Every reference to "this
slice" below means the slice-4 remainder of item 310 under issue #97.

Design only: no compiler change, no `src/` change, nothing implemented.
Parent design: [310-capability-aware-caching.md](310-capability-aware-caching.md)
(read its enforcement-point section first; this note starts where it stops).
Companion docs:
[246-auto-approve.md](246-auto-approve.md),
[245-session-commit.md](245-session-commit.md),
[443-estop.md](443-estop.md),
[414-reach-completeness.md](414-reach-completeness.md),
[teardown-contract.md](teardown-contract.md),
[../crash-recovery.md](../crash-recovery.md).

Source of record for the claims about today's code (line numbers as of
`ee35cea`):

- `src/revl/lower.py:4462-4523`: the extern arm of `_check_cache_declarations`.
  Escrow-shaped externs refuse with G7; every other cache-declaring extern
  refuses with G4 "cache on an interior crossing (extern `X`) is not yet
  enforceable; declare it on the seam method".
- `tests/test_capability_aware_caching_310.py:206`:
  `test_cache_on_an_interior_emission_extern_is_the_later_slice_refusal`,
  the test this slice converts.
- `src/revl/mcp/session.py:2661-2763`: the seam gate in `Session.call`
  (liveness before consumption, `_approval_decide_call` with its `record`
  out-param, `_cache_store` after the body, `_fire_cache_invalidations` at
  return). `2767-2864`: `_grant_live_by_id`, `_cache_entry_live`,
  `_cache_store`, `_fire_cache_invalidations`, `_record_cache_hit`.
- `src/revl/mcp/session.py:3287-3364`: `_approval_decide_call`, the one
  place a call's authority is consumed today.
- `src/revl/mcp/session.py:698-699`: the session installs
  `lease_acquire`/`lease_revoke` on the owner, the shipped shape for a
  runtime question answered by the session's ledger.
- `backends/python/runtime.py:2886-2935`: `Frame.approval_crossing`, the one
  crossing-level ledger transaction the runtime has today. `1926-1939`:
  `_estop_check`. `1942-1964`: `_InFlight`. `2233`: a frame captures
  `_SESSION_OWNER` at construction. `3279-3339`: `SessionOwner`, which
  already carries the typed-approval ledger and the lease callbacks.
- `backends/python/emit.py:1978-1991`: `_emit_fire`, which wraps a fire in
  `_revl_frame.approval_crossing(...)` when the step carries a `with a`
  edge. `1698` and `2463`: the activation-body and provide-method `emit`
  arms. `2060-2121`: `_witnessed_step` and `_method_witnessed_step`.
- `backends/python/replay.py:2103-2140`: `record_approval_consumed`,
  `record_approval_revoked`, `record_approval_emission`. There is NO
  `record_cache_hit` in this file: `Session._record_cache_hit` resolves it
  with `getattr(..., None)` and silently writes nothing (finding 1 below).

## The gap

Item 310 shipped three slices: `cache pure`, `cache capability`, and
`cache external`, all enforced at the SEAM, where the call is the crossing
and the consent gate can decide hit or miss before any body runs. The
parent design derived that restriction from one fact about the shipped
protocol: `_approval_decide_call` consumes a call's authority once, before
`invoke()`, and the runtime that executes the body cannot ask the ledger a
question. A cache-declaring extern reached inside a body is therefore
grammar-legal and admission-refused.

The refused shape is the one the proposal actually named. Registry
resolution, service discovery and agent memory are reads a provider does
in the MIDDLE of a method, next to work that must not be cached:

```revl sketch
extern emission[registry.resolve] fn resolve(name: Str) -> Pin
    cache capability = @py { ... }

service Deploy { emission fn ship(name: Str, rev: Str) -> Receipt }

component D provides deploy: Deploy {
  provide deploy {
    fn ship(name, rev) {
      let pin = emit resolve(name)         // cacheable: a pinned lookup
      emit push(pin, rev)                  // never cacheable: a write
      ...
    }
  }
}
```

`cache capability` on `ship` is a mis-declaration (its reach writes, and
surface H refuses it). The declaration belongs on `resolve`, and today that
is the G4 refusal at `lower.py:4511`. Closing the gap means giving the
runtime the four things the parent design listed as missing, in the order
that keeps 245/246 consent intact.

## Two findings from reading the shipped code

1. **The seam's hit record is a no-op.** Laundering point 5 of the parent
   design ("every hit writes a WAL entry") is implemented as
   `getattr(wal, "record_cache_hit", None)`, and `backends/python/replay.py`
   defines no such method, so a seam hit is counted in `state()` and
   recorded nowhere durable. No test asserts the record's presence, which
   is how it went unnoticed. This slice adds the writer and turns the
   fallback into a direct call (slice 4b), and the exit tests assert the
   record for the SEAM hit path too.
2. **A crossing-level ledger transaction already exists, in one place.**
   `Frame.approval_crossing` is exactly the shape the parent design said
   did not exist: it re-resolves the token AT the crossing (expiry is
   checked there, not at mint, invariant 3), consumes durably before the
   fire through the owner (`owner.consume_approval`), marks the fire
   `_InFlight` so an E-Stop can call it ambiguous, writes the completion
   record after, and refuses MID-BODY with `ApprovalCrossingRefused` when
   the authority died between admission and arrival. The parent design's
   objection to per-crossing consumption ("restructures consent, refuses
   mid-body") describes a protocol the typed-approval path has run since
   item 246. The question for this slice is not whether a mid-body ledger
   transaction is admissible but under which static condition the
   call-level one may be deferred into it.

## The core decision: admission at the seam, consumption at the crossing, under a static cache-only coverage rule

Three candidate placements were weighed. Stated with their defect:

- **A. Keep seam consumption; an interior hit still costs the call its
  use.** Simplest. Sound. But "a hit does not consume `remainingUses`" is
  a stated property of item 310, with an exit test (parent test 10), and
  a count lease that spends one use per call regardless of hits makes
  interior caching worthless under exactly the lease the operator uses to
  bound obtainable results.
- **B. Reserve at the seam, release at return.** Keep today's
  `approval-consumed` record at the seam and write a `cache-release`
  record at call return when the call proved hit-only. Sound, but it
  makes a later record RETRACT an earlier spend. `revl recover` and the
  audit join (`approval-consumed` to `approval-emission` on `requestId`)
  would both need a new rule for a spend that was not one, and a crash
  between the hit and the release reads as consumed-but-unfired, which is
  a lost use with no owed action behind it. A new WAL kind that changes
  the meaning of an existing kind is the wrong spend.
- **C. Defer consumption to the crossing, for cache-only coverage only.**
  Chosen. The seam still ADMITS (classifies, finds live covering
  authority, refuses before any work when there is none), but for a grant
  or approval whose coverage of THIS call's reach is entirely
  cache-declaring extern tokens, the seam records the covering authority
  on a per-call RESERVATION instead of consuming it. The first interior
  MISS under that authority consumes it durably at the crossing,
  consume-before-fire, with the same `approval-consumed` record the seam
  writes today. A hit-only call consumes nothing. No record is ever
  retracted; recovery's rules are unchanged.

Why C keeps 245/246 intact:

- **Refusal before work holds.** An access with no live covering authority
  is refused at the seam, before the body, exactly as today. A mid-body
  refusal can only arise from authority that DIED between admission and
  arrival, which is the case `approval_crossing` already handles mid-body
  today (invariant 3). Nothing that was refused at the seam yesterday is
  admitted to the body tomorrow.
- **Per-call atomicity holds where it held.** Every grant or approval whose
  coverage of the call includes ANY non-cached crossing is consumed at the
  seam, byte-for-byte today's path. The deferral is decided statically per
  (call, authority) pair from the class map's own crossing enumeration, is
  visible in audit, and cannot be reached by a program with no cached
  extern. A non-cache composition's call path is unchanged.
- **The consumption is the same durable spend.** The crossing calls the
  session's own `_consume_grant` / `_consume_approval` /
  `_consume_auto_rule` through the owner. Same `remainingUses` decrement,
  same `consumed` flag, same `approval-consumed` record, same
  fail-closed reading of a crash between spend and fire. One ledger, one
  spend function, two call sites (the item-246 posture: one consent
  mechanism, two entry points).
- **A hit re-delivers an authorized crossing's result.** The entry's
  liveness (`_cache_entry_live`, unchanged) binds it to the grant that
  produced it, its ttl, its covering approval's ttl, its `invalidated_by`
  epochs and its generation. The accessing call additionally holds live
  covering authority of its own (the seam admitted it). Both are checked
  at the crossing, so a hit is reachable only through the same door a
  miss went through.

### The cache-only coverage rule (static)

For a call `(key, method)` under an enabled policy, with reach `R` from the
live class map and a covering authority `a` (a standing grant, a standing
approval, or an auto-approve rule) with capability cone `cone(a)`:

> `a` is DEFERRABLE for the call iff every crossing in `R` that carries a
> token in `cone(a)` is a cache-declaring extern crossing.

"Every crossing" is the class map's per-crossing enumeration
(`ClassMap.crossings_for_capability`), the same closure the a/b/c fold and
surface H fold over, so the rule follows the spawn seam, the transitive
closure and the `*` widening for free (a `*` reach is never cache-only:
the widening carries every token, and some crossing under it is not a
cached extern). Computed once per generation next to
`_install_cache_index`, keyed `(key, method) -> frozenset(cache-only
tokens)`, and consulted by `_approval_decide_call` through one additional
out-param field. Worst-over-reach applies: one uncached crossing under the
cone and the whole authority is consumed at the seam, today's path.

Consequence stated plainly, because it is the one accounting change: a
cache-only-coverage call whose body never arrives at the cached crossing
(a branch not taken) consumes nothing, where today's per-call rule would
have spent a use. A use bounds crossings the operator authorized; a call
that crossed nothing spent nothing. It is the same reasoning that lets a
seam hit skip consumption, and it is unreachable for any program without a
cached extern.

## The runtime infrastructure

Four pieces, each named by the parent design, each specified against the
shipped shape it extends.

### 1. The call reservation (session side)

`Session.call` opens a reservation for the duration of `_run(invoke())`
when, and only when, the call's reach contains a cache-declaring extern
(an index lookup; every other call is untouched):

```
reservation = {
  "key": key, "method": method,
  "deferred": {kind: [ids...]},     # authority admitted but not yet spent
  "consumed": set(),                # ids spent by a miss in this call
  "hits": 0, "misses": 0,
}
```

`_approval_decide_call` fills `record["deferred"]` next to `record["scope"]`
when the coverage rule says an authority is deferrable, and does NOT call
the consume function for it. Reservations form a stack on the session
(`replay_forward` re-enters `Session.call`; a spawn-in-call builds frames
inside the same call), and the owner gate reads the top. Closed in the
same `finally` that clears the session owner. A crossing that finds no
open reservation under an enforced policy refuses (fail closed: a cached
crossing outside any admitted call is exactly the ambient access the
parent design forbids; see the activation-body decision below).

### 2. The owner-carried gate (the question the runtime could not ask)

The session installs one callback on the owner, mirroring
`lease_acquire` at `session.py:698`:

```
owner.cache_gate = self._runtime_cache_gate
# (component, token, extern, args) -> CacheTxn | None
```

The session digests the raw `args` with the same `_cache_args_digest` the
seam uses (`mcp/approval.py`), so the runtime imports nothing from the mcp
package and the two key shapes cannot drift. `CacheTxn` is a small
per-arrival object the session builds against ITS ledger (the entry store
stays on the session; no parallel ledger, the item-294 refusal of parallel
mechanisms applies):

- `txn.hit`: the entry exists, `_cache_entry_live(entry)` is true, and the
  reservation's admitted authority is still live. `txn.value` is the
  stored result.
- `txn.consume()`: the miss path's durable spend. Refuses with
  `CacheCrossingRefused` (a `RuntimeError` subclass beside
  `ApprovalCrossingRefused` and `LeaseRefused` in `runtime.py`, carrying
  the token and the reason) when every
  admitted authority for this call has died since admission, BEFORE any
  spend and before any fire. Otherwise consumes each deferred authority
  not yet consumed in this call (`_consume_grant` and friends, same
  records), moves the ids to `reservation["consumed"]`, and returns the
  scope the entry will bind to. Idempotent per call: the second miss under
  the same authority in one call spends nothing more (a use is per call,
  as today).
- `txn.fill(value)`: `_cache_store` with the returned scope, after the
  fire. Same no-scope rule: a miss that consumed nothing (class none/(a)/
  (b) crossing, or no-policy session) stores nothing.

`None` from the gate (no policy, no class map, no reservation because the
call's reach has no cached extern) means the crossing fires plain: the
no-policy inertness rule of the parent design, extended to the interior.

### 3. The emitted cache-step wrapper (runtime + emitter)

Runtime, in `Frame`, in the mold of `approval_crossing`:

```python
def cache_crossing(self, token, extern, args, fire):
    _estop_check(f"{self.name}.{extern}")                # item 443
    owner = self._owner
    gate = getattr(owner, "cache_gate", None) if owner is not None else None
    if gate is None:
        return fire()                                    # inert
    txn = gate(self.name, token, extern, args)
    if txn is None:
        return fire()
    if txn.hit:
        txn.record_hit()                                 # WAL, counter
        return txn.value
    scope = txn.consume()                                # may refuse; spend before fire
    with _InFlight(component=self.name, method=extern,
                   seq=scope_request_id(scope), entry="crossing"):
        result = fire()
    txn.fill(result)                                     # WAL fill record, entry store
    return result
```

Emitter (`backends/python/emit.py`, `_emit_fire`): when the fired call
names a cache-declaring extern (the IR extern entry gains the `cache`
field at `lower.py:4188`, the same `_cache_ir` descriptor fns and methods
carry), the fire becomes

```python
(lambda _revl_a: _revl_frame.cache_crossing("registry.resolve", "resolve",
                 _revl_a, lambda: resolve(*_revl_a)))((name,))
```

The expression form keeps `_emit_fire` an expression (it is used in
expression-bodied methods such as `fn get(id) = emit read_db(id)`), and
the outer lambda evaluates the arguments exactly once for both the digest
and the fire. When the step also carries a `with a` approval edge, the
cache wrapper is OUTSIDE the `approval_crossing` wrapper: a hit crosses
nothing and so consumes no typed approval either; a miss runs today's
approval crossing unchanged inside the fire thunk. `_witnessed_step`'s
mold (a per-step emitter that binds a temp and branches on `Ok`) is not
needed: a cache-declaring extern is never witnessed (refused at
`lower.py:4469`), so the wrapper has no `Ok`-conditional registration to
emit.

Where `_revl_frame` is not in scope the wrapper cannot be emitted, and
silently emitting the plain call would erase the declaration (the lesson
the 231a inliner exclusion already encodes). Two static refusals follow,
both in `_check_cache_declarations`, both new, both pointed:

- a cache-declaring extern reached from a PLAIN `fn` body ("cached extern
  `resolve` is reached from fn `lookup`, which has no activation frame to
  settle the crossing against; call it from a component body or a provide
  method"), computed from `emitting_caps` per fn;
- a cache-declaring extern reached from an ACTIVATION body. The activation
  gate (`_enforce_activation_gate`) consumes for the activation reach at
  load, with no per-call reservation to defer into. Refused in this slice
  with a message naming the load-time gate; lifting it is open question 1.

Non-py tiers: every tier without a session-owner runtime refuses a
cache-declaring extern at emit, exactly as item 245's tier gate refuses a
`deferred` extern there. Honest and visible, never silently uncached.

### 4. WAL ordering and the two new record kinds

Two additive record kinds in `backends/python/replay.py`, both facts about
existing seqs (they consume no seq, like `approval-emission`):

- `cache-fill {digest, token, extern, requestIds, component}`: written by
  `txn.fill`, after the fire, naming the authority the miss consumed.
- `cache-hit {digest, token, extern, requestIds, component}`: written by
  `txn.record_hit`, naming the fill it re-delivers.

The seam path gains the same `cache-hit` record (finding 1), with
`key`/`method` in place of `token`/`extern`.

The ordering invariants, per call, in one WAL:

1. Seam consumption first. Every `approval-consumed` the seam writes for a
   non-deferred authority precedes the body's first record.
2. Consume before fill. A `cache-fill` naming `requestId` r is preceded,
   within the same call, by an `approval-consumed` r (the deferred spend at
   the crossing) or by the seam's own `approval-consumed` r (when the
   authority was not deferrable). A fill with no spend behind it is
   impossible by construction (no scope, no store), and `read_wal`
   (`src/revl/wal.py`) raises `WALIntegrityError` if it ever observes one.
3. Fill before hit. A `cache-hit` for digest d follows a `cache-fill` for d
   in the log. A hit for an entry the log never saw filled is a violation.
4. A hit-only call writes no `approval-consumed` for a deferred authority.

A crash between `approval-consumed` and `cache-fill` reads, on recover, as
consumed-but-unfired: an owed action needing fresh approval, today's
fail-closed rule, unchanged. `revl recover` learns nothing new; it ignores
both new kinds as it ignores `approval-emission`. `revl audit` joins them:
a fill with its spend, a hit with its fill, and prints the cache line on
the extern boundary (class, clauses, `consumption: crossing` for a
cache-only-coverage seam, `consumption: call` otherwise).

`invalidated_by` ordering has one new edge. The seam fires invalidations at
call RETURN (`_fire_cache_invalidations`), which is correct across calls
and wrong within one: a body that crosses `user.updated` and then reads a
cached `get_profile` in the SAME call would read its own stale entry.
Uncached emissions have no runtime hook to bump the epoch at their
crossing, so this slice refuses the shape statically: a method whose reach
contains both a cache-declaring extern and a crossing of one of that
extern's `invalidated_by` tokens is refused at admission ("split the
write and the read across methods, or drop the subscription"). Cheap,
honest, and the same-call case is exactly the one an author would get
wrong. Lifting it by wrapping subscribed invalidating externs is slice 4c.

### Mid-execution refusal semantics

`CacheCrossingRefused` fires at the crossing, on the miss path, before any
spend and before any fire, when the call's admitted authority has died
since the seam admitted it (revocation, `expiresAt`, exhaustion by a
concurrent session on a shared WAL is not possible, sessions are
single-process; the concrete case is an operator `revl revoke` from
another verb between admission and arrival). It propagates out of the
emitted body exactly as `ApprovalCrossingRefused` does today:

- entries already registered on the frame (transactional, compensation,
  brackets) are on the activation stack and settle under the session
  verdict; G7's completeness and soundness are unchanged, nothing is
  dropped, and an abort replays them LIFO. The parent design's "partial
  work already escrowed" is precisely the case the escrow exists for;
- the `_InFlight` window is empty at the refusal point (the refusal
  precedes the `with`), so an E-Stop overlapping it is unambiguous:
  nothing was attempted;
- `Session.call` propagates the exception; the reservation is closed in
  the `finally`; the `state()` counter `cacheInterior.refusals` increments;
  no `cache-fill` and no `approval-consumed` were written for the refused
  arrival.

A hit path never refuses mid-body on authority grounds: a dead entry is a
miss, and the miss path decides. A hit path CAN raise `EstopHalted`
(`_estop_check` runs first), which is item 443's contract at every seam.

## What stays exactly as it is

- Static reach. `cache` on an extern is metadata (`emission_analysis.py`
  does not read it). Every 414 surface sees the cached crossing on the
  hit path and the miss path identically. The CROSSING_KINDS enumeration
  in `tests/test_reach_completeness.py` keys on HOW authority is reached,
  and an interior cached extern is reached as the existing `extern` kind;
  no new kind is minted. The guard this slice adds is an INERTNESS cell:
  for every reach surface, the reach set with `cache` on the interior
  extern equals the reach set without it.
- Admission classification, the ticket two-step, `_find_standing_grant`'s
  recorded-grant rule (an entry binds to the ids the miss consumed, never
  to any authority that could have covered), the no-policy inertness rule,
  the escrow-shaped refusals, the structural resource walk (now applied to
  the extern's params and return), the distributed-placement refusal (now
  naming a cached extern the placement splits from its WAL), surface H.
- `_cache_entry_live`, `_cache_store`, `_grant_live_by_id`: reused as they
  are. Interior entries live in the same `_cache_entries` dict under a
  distinct key shape `("extern", name, digest)` and die with the same
  events.
- A seam method that is itself cache-declared and whose reach includes a
  cache-declaring extern: admitted. A seam hit never reaches the interior;
  a seam miss populates both. Harmless, and refusing it would force an
  author to pick one declaration for two different staleness facts.

## Slice plan

Ordered by dependency; out of order is refused.

- **Slice 4a: the transaction.** Lift the G4 refusal for emission externs;
  `cache` on the extern IR entry; the plain-fn-reach, activation-body,
  same-call-invalidation, and non-py-tier refusals; the resource walk on
  extern params/return; the cache-only coverage index next to
  `_install_cache_index`; the reservation stack and the deferred
  consumption branch in `_approval_decide_call`; `owner.cache_gate` and
  `CacheTxn`; `Frame.cache_crossing`; the `_emit_fire` wrapper;
  `CacheCrossingRefused`; `state()` counters `cacheInterior.{hits, misses,
  refusals}`; the call result carries `cacheInterior: {hits, misses}` only
  when either is non-zero. Liveness, ttl, cross-call `invalidated_by` and
  approval-ttl coupling come free through `_cache_entry_live`. Exit tests
  1 to 8 and 12. py tier only.
- **Slice 4b: the record.** `record_cache_fill` / `record_cache_hit` in
  `replay.py`, `read_wal` recognition, the seam's direct call (finding 1),
  ordering invariants 2 and 3 enforced in `read_wal` as
  `WALIntegrityError`, the audit join and
  the extern cache line with `consumption: crossing|call`. Exit tests 9
  to 11 and the seam regression test 13.
- **Slice 4c: the polish.** Same-call invalidation ordering by wrapping
  subscribed invalidating externs in a `_revl_frame.invalidating_crossing`
  that bumps the epoch at the crossing (lifting the 4a static refusal);
  `cache pure` on a `pure` extern through the existing
  `_emit_memo_wrapper` (no ledger interaction; the host body's purity is
  the author's `pure` claim, the 44/309 tier, as it is for every pure
  extern today). Exit tests 14 and 15.

The self-host question, asked per feature: the `cache` clause is not in the
self-host grammar (the surface-H landing recorded that), so this slice
needs no self-host port. The 414 guard is the inertness cell, not a new
row.

## Exit tests

The refusal test at `tests/test_capability_aware_caching_310.py:206` is
DELETED and replaced by the suite below, in the same file under a new
"interior crossings" section (the item number 310 is final; only this
note's id is provisional). Fixture: an unscoped `extern emission fn
read_db(sink, id)` gains `cache capability`, the seam method `Users.get`
loses its clause, and the provider body is `fn get(sink, id) = emit
read_db(sink, id)`; the host body appends `r:<id>` to `sink` so the miss
count is observable, as the seam suite does.

1. **Interior hit and miss.** Under a grant with `uses=5`: miss, hit,
   distinct-args miss. `_reads(sink) == 2`; `state()["cacheInterior"]` is
   `{hits: 1, misses: 2, refusals: 0}`; the second call's result carries
   `cacheInterior: {hits: 1, misses: 0}` and no `cacheHit` key (the CALL
   was not a hit); the first and third carry `misses: 1`.
2. **Hit does not consume.** After the three calls of test 1
   `remainingUses == 3` and `grantsConsumed == 2`: the hit-only call spent
   nothing and the seam wrote no `approval-consumed` for it.
3. **No laundering.** An ungranted access is refused at the seam with
   `ApprovalRequired` and `_reads == 0`; after a grant, miss then hit;
   after `revoke_standing_grant`, the next access is refused at the seam,
   `_reads` unchanged, and no `cache-hit` was recorded for it.
4. **Revoke forces a miss under two covering grants.** Grants GA and GB
   both cover `read_db`; a miss binds the entry to both ids (the seam
   slice's `grantIds` rule); revoking GA kills the entry; the next access
   MISSES (`_reads` increments), consumes GB at the crossing, and re-keys
   to `[GB]`; a further access hits; revoking GB then refuses at the seam.
5. **Mid-call death refuses before spend.** The provider body calls an
   uncached pure extern whose `@py` body invokes a test-registered hook
   that revokes the grant, then `emit read_db(...)`. `Session.call` raises
   `CacheCrossingRefused`; `_reads` unchanged; `grantsConsumed` unchanged;
   no `cache-fill` and no `approval-consumed` in the WAL for that call; a
   witnessed extern fired earlier in the same body has its inverse replayed
   on `abort` (nothing escrowed was dropped).
6. **Cache-only coverage is worst-over-reach.** A provider whose body
   crosses `read_db` (cached) AND an uncached emission under the same grant
   cone consumes at the seam: after a call whose cached arrival hits,
   `remainingUses` still decremented by one, and the audit line reads
   `consumption: call`. The same program with the uncached emission
   removed reads `consumption: crossing` and does not decrement on a hit.
7. **Static refusals.** A cache-declaring extern reached from a plain `fn`
   refuses naming the fn; reached from an activation body refuses naming
   the load gate; a method whose reach crosses an `invalidated_by` token of
   a cached extern it also reads refuses naming both crossings; a split
   placement (distribute.py) refuses naming the extern; each non-py tier
   refuses at emit. `cache` on a witnessed / acquire / deferred /
   compensate extern keeps its G7 refusal (unchanged tests). A
   resource-carrying param or return on a cached extern refuses with the
   structural walk.
8. **Inertness.** For each reach surface in `tests/test_reach_completeness.py`
   (`boundary`, `component_reach`, `approval`, `taint`), the reach set with
   `cache capability` on the interior extern equals the set without it. A
   program with no cached extern emits byte-identical Python, an identical
   IR, and an identical WAL; a no-policy session takes the miss path on
   every access (`_reads` increments per call) and stores nothing.
9. **WAL order, miss then hit.** The records for the two calls, filtered to
   the cache and approval kinds, are exactly `[approval-consumed r,
   cache-fill d r, cache-hit d r]` in that order; invariants 2 and 3 hold
   over the whole log; the hit-only call added no `approval-consumed`.
10. **WAL order, seam consumption first.** In the test-6 program, every
    `approval-consumed` for the non-deferred authority precedes the body's
    first `cache-*` record within the call.
11. **`read_wal` refuses the impossible shapes.** A hand-built log with a
    `cache-hit` before its `cache-fill`, and one with a `cache-fill` with no
    `approval-consumed` for its `requestId`, both raise `WALIntegrityError`
    naming the invariant (the `tests/test_wal_integrity.py` shape).
12. **Freshness on the interior.** `cache external ttl 5m` on the extern:
    a hit inside the ttl, a miss past it (injected clock). `cache external
    invalidated_by user.updated`: a crossing of `user.updated` in a
    DIFFERENT call forces the next access to miss; the same-call shape is
    the static refusal of test 7. For an approval-required token, the miss
    consumes the single-use approval at the crossing, same-args hits
    continue until the approval's ttl, a different-args access prompts
    again, and a hit-only call before any miss is impossible (no entry).
13. **Seam hit record regression.** A SEAM `cache capability` hit writes a
    `cache-hit` record to the WAL (today it writes nothing; the test fails
    on main until 4b lands).
14. **Same-call invalidation (4c).** With the static refusal lifted, a body
    that crosses `user.updated` then reads the cached extern in the same
    call observes the fresh value, and the epoch bump precedes the read in
    the WAL.
15. **`cache pure` on a pure extern (4c).** Host-visible evaluation count
    is 1 across repeated same-args calls, 2 across distinct args; no ledger
    interaction; identical on and off policy.

## Scoped out (and to whom)

- Activation-body cached crossings: open question 1, this note.
- Non-py tiers: each tier's session-owner runtime is its own item; until
  then the emit-time refusal is the contract.
- Cross-composition invalidation and everything the parent design scoped
  to the federation surface.
- Sampling revalidation for the elided-write mis-declaration (parent open
  question 4): unchanged by this slice, and if it lands it lands at
  `Frame.cache_crossing` as a would-be-hit that takes the miss path.

## Open questions

1. Should an activation-body cached crossing defer into the ACTIVATION
   gate's decision (a reservation opened around `load` and `swap` with the
   gate's consumed scope) rather than refuse? It is the same mechanism with
   a different opener; the reason to wait is that the activation gate is
   all-or-nothing across components (issue #204) and a deferred spend inside
   it needs a rule for a load that partially fails.
2. Should a cache-only-coverage call that never arrives at the crossing
   consume nothing (this note's answer) or one use (today's per-call
   accounting)? The note picks "nothing" because a use bounds crossings;
   revisit if an operator lease policy turns out to count calls on purpose.
3. Reservation stack depth: is there any admitted path where two
   reservations are open at once with DIFFERENT authority (a
   `replay_forward` inside a call under a different grant)? The gate reads
   the top; if the answer is yes the entry must bind to the top's ids, and
   a test should pin it.
