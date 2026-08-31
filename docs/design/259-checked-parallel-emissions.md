# Checked parallel emissions - the latency answer, derived from declarations (item 259)

**Status: design, not implemented.** This document specifies how the checker
derives a parallelizable *partition* of a body's emission sequence from the
capability declarations already present, and how the py and ts runtimes fan
each parallel group out concurrently while producing byte-identical results,
a byte-identical audit surface, and an unchanged LIFO teardown. There is no new
syntax: the only declaration involved is `commutative` (Def. 39), which today
upgrades recovery ordering and here also earns an execution payoff. Anything not
provably independent stays sequential, silently.

The headline finding from the adversarial self-review (S7) is stated up front
because it constrains every other section: **the audit surface is not produced
only at the revl seam - host extern bodies emit their own trace records
mid-call (`record(...)` inside `PoolHandle.query` on ts, `_record` observers on
py), so naive concurrency interleaves those records nondeterministically and the
"same audit surface" exit criterion fails on the first parallel group.** The
design's answer is a per-branch audit capture that replays in sequential plan
order at the join (S4, S7-C1). Read S7 before S4.

---

## 0. Revision (adversarial review 2026-08-31)

A second, independent adversarial review found one NEW CRITICAL beyond the
author's self-review, two HIGHs, and a MEDIUM/HIGH. This section states what
changed and the corrected central claim; the affected sections below (S2.1, S2.3,
S3.2, S3.3, S4.1, S5, S8, S9) carry the detail.

**Corrected central claim.** The prior claim was that concurrent execution of a
group yields a result, audit trace, and teardown that are **byte-identical** to
sequential, including a byte-identical `FaultProbe.accumulated`. That claim is
**withdrawn for `accumulated`**. Byte-identity of `accumulated` needs the same
*set* of registrations, and the prior design defended only their *order*. Two
paths change the set (the NEW CRITICAL below), so no group that can fault or be
diverted can promise a byte-identical `accumulated`. The corrected exit criterion
is **teardown-EFFECT equivalence**: after any teardown (clean commit, internal
fault, or external divert), the world ends in the same state a sequential run
would leave it in, and the compensations that *do* run are registered in plan
order. This is strictly weaker than byte-identical `accumulated` and is what the
conservative slice-1 parallelizable set is chosen to guarantee.

**NEW CRITICAL - the fault/divert path changes the registered set; "await the
whole group" opens a divert hole.** S3.3's invariant P defends registration
*order* but not the registration *set*. Two triggers change the set:

1. *Internal fault.* Group `[e1, e2, e3]`, `e2` raises. Sequential leaves
   `accumulated=[e1]` (e3 never runs). Parallel awaits the whole group and
   registers every successful branch, so `accumulated=[e1, e3]`. C2's idempotent
   gate covers only e3's FORWARD delivery being a benign re-send; it says nothing
   about e3's COMPENSATION. An idempotent "send welcome" can carry a real
   "send retraction" compensation, so parallel teardown runs a compensation
   sequential never would. This fails the design's own S9 fault exit test.
2. *External A1 divert / cancellation.* A deadline expiry, a sibling-component
   fault, or an explicit cancel that lands at an `await` mid-run makes SEQUENTIAL
   execution SKIP every remaining emission (`backends/python/replay.py:936`,
   "a close during an await skips the later steps"). A straight-line run of three
   400ms emissions takes >1s, so a mid-run divert is the NORMAL teardown trigger,
   not an edge case. The parallel shape collapses all three into one
   `await _revl_parallel(...)` that S5 refuses to abandon, so the divert is
   suppressed until the whole group fires: `accumulated=[e1, e2, e3]` vs
   sequential `[e1]`, plus G4 over-emission. C2's idempotent gate does not even
   engage, because a divert is not a branch fault. The prior doc never mentioned
   divert, cancel, deadline, or A1 at all - that silence is the core defect.

*Resolution (conservative slice-1 path).* S3.3, S5, and S9 are revised to:
DROP the byte-identical `accumulated` claim; restrict a multi-emission group to
emissions whose COMPENSATION is itself **idempotent-or-absent** (not merely
idempotent forward delivery); and re-state the exit criterion as teardown-EFFECT
equivalence. A group is only formed from emissions safe to over-fire under a
divert, which idempotent-or-absent compensation gives us: over-firing e3 and then
running its (idempotent-or-absent) compensation leaves the same world state as
never firing it. A **divert-aware `_revl_parallel`** (on `GeneratorExit` /
`CancelledError`, propagate cancellation to un-started branches, await only
started ones, register only members that actually fired) is noted as a later
option, but it makes the fired set nondeterministic, so slice 1 takes the
conservative restriction instead.

**HIGH-1 - the task-local record sink cannot be built on the current `_record`.**
`_record` (`backends/python/runtime.py:420`) reads MODULE GLOBALS `_trace` and
`_observers`; it is not task-local, so three interleaving branches cannot each
hold a distinct sink. Even rebuilt as a `contextvar`, records escape the branch
task when emitted OFF the awaiting task: `Clock.fire` (~2661), `Job` completion
(`job.run ... done` ~2560), and `_revl_record_spawn` (~358) each call `_record`
from a context that is not the branch's task. S3.2/S4.1 are revised to spec the
`_record` change explicitly (a contextvar-backed sink STACK, pushed on branch
entry and popped at flush) AND to restrict slice-2 parallelization to emissions
all of whose audit records are emitted SYNCHRONOUSLY within the awaiting task
(timer/job/spawn-routed record producers are excluded). C1 holds only under this
refactor plus this synchronicity restriction.

**HIGH-2 - a first-class arrow emission is invisible to the three-arm walk.** The
`*`->singleton defense only fires if the site appears in the emission sequence
carrying `*`. But `__main__._boundary`'s three `walk_expr` arms recognize only a
`req`-target call and an `instance-get` provision call (~139/157); a first-class
emitting arrow is detected only by `_emitting_capabilities` / `unknown_dispatch`
at the **fn-aggregate** level (~125/195), not per step. `parallel.py` reusing the
three arms inherits that blindness, so an opaque arrow-typed call sitting between
two disjoint emissions is treated as a non-barrier and the two are grouped and
reordered around a hidden crossing. S2.1 is revised to add an explicit BARRIER:
any step whose callee reach includes the fn-caps `*` / `unknown_dispatch` hard-
breaks the straight-line run. The three-arm walk is not trusted to surface
first-class emissions.

**MEDIUM/HIGH - Def-39 `commutative` conflated with concurrent forward-execution
safety.** `commutative` declares the final state independent of teardown/RECOVERY
ORDER. That does not imply interleave-safety of concurrent FORWARD firing: a
non-atomic read-modify-write host op behind the same key loses updates under
interleaving. C4 only catches a LYING commutative, not this reorder-vs-interleave
category error for an HONEST one. S2.3 is revised to state and DEFEND the narrower
theorem "recovery-commutative implies forward-interleave-safe UNDER THE
SINGLE-LOOP no-true-parallelism model," and to document that same-key concurrency
rests on there being no true parallelism (a threaded runtime could not use it).

**Kept as disclosed - C3 (declared-tokens-only disjointness).** The review
confirmed C3 is honestly disclosed as an accepted OPEN residual, the same
"declaration is complete" trust G4/414/256 already rest on; slice-1's D1-only
fail-safe is as safe as the capability system and no safer. The wording stays.

**Re-slice.** Slice 1 stays checker-only (partition + audit-surface render, no
runtime change) and lands alone, now with the corrected barrier set (the
first-class `*` barrier) and the corrected exit criterion (teardown-EFFECT
equivalence, not byte-identical `accumulated`). Slice 2 (py runtime fan-out)
carries the contextvar record-sink STACK refactor, the compensation-idempotence
(idempotent-or-absent) restriction, and divert handling.

---

## 1. What this is, and what it is not

An agent turn's wall-clock is dominated by sequential tool calls: three
independent `emit`s that each wait 400ms on a host round trip cost 1200ms
end to end, even though nothing about the second depends on the first. Every
harness that parallelizes tool calls does it by vibes (a hand-maintained "these
are safe to batch" list) or not at all.

revl already carries the fact that decides safety: an emission's declared
capability set. Item 294 made a capability a point in a partial order
(`cap_order.Cap`, `covers`/`covers_set`); item 246 made every emission crossing
enumerable at G8 (`__main__._boundary`); Def. 39 made order-independence a
declaration (`commutative`). This item joins those three: the checker reads the
emission sequence off the IR, proves which contiguous runs are pairwise
independent, and hands the runtime a *plan* - an ordered list of groups, each
group a set of emissions the runtime may fire concurrently. The runtime fans
each group out and rejoins it so that results, audit, and teardown are
bit-identical to running the whole body sequentially.

This is **not**:

- **new syntax.** No `parallel { }` block, no `spawn`-style annotation, no
  `@parallel`. The partition is derived; the author writes an ordinary body.
- **an unsafe opt-in.** There is no "trust me" escape hatch. A group that
  cannot be *proved* independent stays sequential. The default is fail-safe.
- **speculative or reordering execution of dependent work.** Dependent
  emissions keep their program order exactly.
- **a change to the capability algebra.** Disjointness is defined on top of the
  existing `cap_order` relation; no new `Cap` kind, no registry row.

The exit test (SS9) is the roadmap's: a body with three disjoint-capability
emissions completes in `~max(latencies)` not `sum`, byte-identical results and
audit, on py and ts; a non-commutative same-key pair is proven to stay ordered.

---

## 2. The partition derivation

### 2.1 The emission sequence, read off the IR

The unit of partitioning is one **straight-line run of emission steps** inside a
single activation or provide-method body. The sequence is exactly what
`__main__._boundary`'s `walk_steps`/`walk_expr` already enumerate, in body
order, over three crossing shapes (mirrored line-for-line, the same way
`cardinality.py` mirrors them so no fourth shape is silently missed):

1. an `emit` step (`{"step": "emit", ...}`, lowered by
   `lower._lower_emit_step`), including the `await emit` async spelling;
2. a `req`-target emission call (`call` whose `target.kind == "req"` and whose
   resolved service method has `spec["emission"]`);
3. a spawn-handle provision-method call `s.<key>.<method>(...)` reached through
   an `instance-get` receiver (the item 246 seam).

Each emission site `e` carries a **declared capability set** `caps(e)`: the
`Cap`s from the service method's `capabilities` list (or `{Cap("*", ())}` when
the method is a bare `emission` with no scope), parsed through
`cap_order.parse_cap`. This is the identical set `_boundary` records into its
`capabilities` map today; the partition consumes it rather than recomputing a
second, divergent surface (the SS5 soundness tie).

The sequence is broken at any step that is **not** a pure or emission step -
a control-flow join (`if` with emissions in an arm), a loop boundary, an
`await` on a non-emission value, a `provide`/`spawn`/`effect` registration, a
`let` whose value a later emission reads. Those boundaries reset the run to
length zero. A parallel group never spans one. This keeps slice 1 to
straight-line emission runs, where independence is a local property; branches
and loops are a documented follow-on (S7, S10).

**The first-class-emission barrier (HIGH-2, adversarial review 2026-08-31).**
The three shapes above are exactly the arms `__main__._boundary`'s `walk_expr`
recognizes, and that is the problem: those arms surface only a `req`-target call
and an `instance-get` provision call. An emission reached through a **first-class
arrow** (an emitting callable handed to a dispatcher, `f(x)` through an
arrow-typed parameter) is named in no call position, so the three-arm walk never
sees it. `__main__` catches that case only at the fn-aggregate level, through
`_emitting_capabilities` / `unknown_dispatch` (the `*` fixed point, ~125/195),
NOT per step. A `parallel.py` that reused the three arms alone would treat an
opaque arrow-typed call sitting *between* two disjoint emissions as a non-barrier
and group-and-reorder the two around a hidden crossing. So the derivation adds an
explicit **barrier**, over and above the three-arm surface:

> **B (first-class barrier).** Any step whose callee reach includes the fn-caps
> `*` / `unknown_dispatch` marker (as computed by `_emitting_capabilities` over
> the enclosing fn, the identical fixed point `_boundary` consults for its
> `unknown_dispatch` flag) **hard-breaks** the straight-line run, exactly like a
> control-flow join. The run resets to length zero across it.

This means `parallel.py` must consult the fn-aggregate `unknown_dispatch`
surface, not only the per-step three-arm walk. A step that *might* cross an
unnameable boundary can never be inside a group, so a hidden first-class emission
can never be reordered around. The `*`->singleton rule of S2.2 handles a
first-class emission the walk *does* surface carrying `*`; barrier B handles the
one it does not surface at all. The two together close the first-class hole.

### 2.2 When two emissions are provably independent

Two emissions `e1`, `e2` are **independent** when their declared capability
sets are pairwise disjoint *and* neither set names an unnameable reach:

```
independent(e1, e2)  :=
    "*" not in tokens(caps(e1))  and  "*" not in tokens(caps(e2))
    and  for all a in caps(e1), b in caps(e2):  disjoint(a, b)
```

`disjoint(a, b)` is a new predicate added to `cap_order.py`, defined on the
existing partial order so the algebra stays in one place:

```
disjoint(a, b)  :=
    a.token != "*" and b.token != "*" and (
        a.token != b.token                       # (D1) distinct tokens
        or  no_common_lower_bound(a, b)          # (D2) same token, sibling cones
    )
```

- **(D1) distinct tokens.** Capabilities under different tokens are
  incomparable in the partial order (`covers` clause 1: "parameterization never
  bridges tokens"), so `send.email` and `db.write` can never touch the same
  declared resource. This is the whole of slice 1's independence proof.
- **(D2) same token, disjoint cones** (slice 2 refinement, specified here so
  the predicate is complete but *gated off* in slice 1). Two `fs.write` caps
  are disjoint when their resource valuations have no common lower bound: two
  sibling paths (`path="/a"` vs `path="/b"`, neither a prefix of the other under
  `_leq_path`), two distinct discrete values (`host="x"` vs `host="y"`). A
  ceiling parameter (`calls`, `size`) is *not* a resource and never separates
  cones (`is_ceiling`), so it is projected out with `split_ceilings` before the
  cone test - exactly as the crossing-coverage surface projects it out. Slice 1
  treats every same-token pair as **not** disjoint, so a `fs.write(path="/a")` /
  `fs.write(path="/b")` pair stays sequential until slice 2 turns (D2) on. This
  is fail-safe: (D2) can only ever *add* parallelism, never remove a
  sequential guarantee.

`*` (an unscoped `emission`, or a first-class dispatch the G4 fixed point marks
unnameable) is **top of the order and disjoint from nothing** - it may reach any
boundary, so it is never provably independent of anything, including another
`*`. It forces its emission to a singleton group (SS6, attack 5). This is the
same "`*` is the capability no `emission[...]` list can name" rule the G4/G8
surface already relies on.

### 2.3 When two emissions are reorderable by commutativity

Independence (disjoint caps) is *sufficient but not necessary* for concurrency.
Two emissions that cross the **same** key are not disjoint, but they are safe to
overlap when the operation promises order-independence:

```
reorderable(e1, e2)  :=  same_key(e1, e2)  and  commutative(e1)  and  commutative(e2)
```

where `commutative(e)` reads the `commutative` flag off the resolved service or
method spec (the same flag `admission.py` threads as `commutative=bool(mspec...)`
and `adapt.py` checks for a `commutative-mismatch` at the seam). `same_key`
means the two crossings resolve to the identical `Cap` (same token, same
valuation) - a genuine reuse of one boundary, not two disjoint cones.

Def. 39 today upgrades only *recovery* ordering: a `commutative` key may be torn
down in an order other than strict LIFO. This item gives the declaration its
**execution** payoff: a same-key `commutative` run may fire concurrently and
rejoin in any completion order. A same-key run where either side is **not**
`commutative` stays strictly sequential - proven ordered, which is the exit
test's second half.

**Reorderability is not interleave-safety (MEDIUM/HIGH, adversarial review
2026-08-31).** The step above is a category error unless it is defended. Def-39
`commutative` declares that the final state is independent of teardown/RECOVERY
ORDER - the order compensations are *replayed*. It does **not**, on its face,
declare that concurrent FORWARD firing is interleave-safe: a host op that is a
non-atomic read-modify-write behind the same key (read counter, add, write) loses
an update when two such crossings interleave their `await` points, even though
either serial order is fine and either recovery order is fine. C4 (S7) catches a
*lying* `commutative`; it does not catch this reorder-vs-interleave gap for an
*honest* one. Two ways to close it; this design takes the second and makes the
resting assumption explicit rather than inventing a new declaration:

- Introduce a distinct `interleave-safe` predicate, separate from Def-39
  `commutative`. Rejected for slice 1: it is a new declaration surface with no
  seam check, no version story, and no `adapt.py` mismatch, so it would be a
  "trust me" flag - exactly what S1 forbids.
- **State and defend the theorem:** *recovery-commutative implies
  forward-interleave-safe UNDER the single-loop no-true-parallelism model.* On py
  (cordis-py) and ts (cordis) there is one cooperative event loop and no true
  parallelism (`schedule.py` design decision 3). "Concurrent" therefore means the
  branches' `await` points interleave on ONE loop: between two `await` yields, a
  branch runs to its next suspension **atomically** with respect to every other
  branch. A read-modify-write that a host op performs *without* an intervening
  `await` is thus indivisible under this model, so two same-key crossings cannot
  lose an update to each other; the only reordering visible to host state is the
  order in which the crossings' `await` boundaries resolve, which is precisely the
  order `commutative` already promises is immaterial. Hence, on the single-loop
  model, recovery-commutativity is sufficient for forward-interleave-safety.

**Resting assumption, stated.** This theorem rests entirely on there being no
true parallelism. A threaded or multi-process runtime could preempt a host op
mid read-modify-write, breaking the atomicity the argument depends on, so such a
runtime **could not** use the same-key `commutative` payoff and would have to fall
back to the disjoint-caps (D1/D2) path or stay sequential. Slice 1 and slice 2
target only the single-loop cordis tiers, where the assumption holds; the payoff
is documented as conditional on it.

### 2.4 The partition algorithm

The parallel groups are the connected components of the "may-overlap" relation,
restricted so a group never reorders a *dependent* pair. Over one straight-line
emission run `E = [e_1, ..., e_n]` in body order:

```
sketch  (checker-side, new module src/revl/parallel.py)

  groups = []
  current = [e_1]
  for e in e_2 .. e_n:
      if for every m in current:  independent(m, e) or reorderable(m, e):
          current.append(e)          # e joins the running group
      else:
          groups.append(current)     # e depends on something in `current`
          current = [e]              # start a fresh group at e
  groups.append(current)
```

A group grows only while every new emission is *pairwise* compatible (disjoint
or same-key-commutative) with **every** emission already in it - not just the
previous one. This is the key soundness constraint: a chain `a - b - c` where
`a` and `c` share a key but `b` is disjoint from both must not put `a` and `c`
in one concurrent group unless `a`/`c` are also `commutative`. Requiring
pairwise compatibility across the whole running group closes that hole; the
first incompatible emission seals the current group and opens the next, so the
groups stay a *contiguous partition* of the original order. Group boundaries are
therefore sequential barriers: group `k+1` does not start until group `k` has
fully rejoined (SS4), which is what lets acquisition order be recorded as plan
order (SS3).

The plan attached to the body IR is the ordered list of groups, each group an
ordered list of emission-step indices:

```
"parallel_plan": [ {"group": [0]}, {"group": [1, 2, 3]}, {"group": [4]} ]
```

A group of size 1 is a plain sequential emission - byte-identical IR consumers
that ignore the key see no change, so the plan is additive.

---

## 3. Soundness: same results, same audit, unchanged teardown (the crux)

The claim is that for a straight-line run partitioned into groups, executing
each group concurrently yields a result, an audit trace, and a teardown order
byte-identical to executing the whole run sequentially. Three obligations, and
the crux is the third.

### 3.1 Results are identical

Within a group, every pair is either disjoint (no shared declared resource, so
neither call's host effect can be an input to the other's result) or same-key
`commutative` (the author declared the final state independent of order). No
member reads a `let` bound by another member (a `let`-dependence is a sequence
break, SS2.1), and no member's control flow guards another (straight-line only).
So each call computes the identical value it would compute alone, and the
group's result vector is order-independent. The runtime binds results back to
their `let`s **in plan order** at the join, so any later step reads the same
bindings it would read sequentially. Result identity holds on the **success
path**; the fault path is the subject of SS8-C2 and is *not* byte-identical
without the mitigation there.

### 3.2 Audit is identical - but only because of buffered replay

The audit surface (`revl audit --json` at build time; the runtime `hostLog` /
`_record` trace at run time, diffed by `audit_diff.py`) must be byte-identical.
The build-time surface is trivially unchanged: `_boundary` already records
emissions into an *order-insensitive set* keyed by label, and the
`parallel_plan` key is additive. The **run-time trace** is the hard part and is
the headline finding (S7-C1): host extern bodies write their own records
*mid-call*, so concurrency interleaves them. The design's answer is that each
parallel branch runs with a **branch-local record sink**; at the join the runtime
concatenates the branches' captured records **in plan order** and only then
emits them to the real `_record`/`hostLog`. The observable trace is therefore
the plan-order concatenation - byte-identical to sequential - even though the
host calls overlapped in wall-clock. S4 specifies the sink; S7-C1 states the
residual risk (a trace that *encodes* a true cross-branch host order the replay
now hides).

**Why "task-local" is not enough, and the synchronicity restriction (HIGH-1,
adversarial review 2026-08-31).** The prior draft said "task-local record sink"
as if it could be dropped onto the existing `_record`. It cannot, for two
reasons grounded in `backends/python/runtime.py`:

1. `_record` (~420) reads the **module globals** `_trace` (~381) and `_observers`
   (~369). There is no task-local dimension at all, so three interleaving
   branches cannot each hold a distinct sink by construction. The sink has to be
   *built*: `_record` must consult a **contextvar-backed sink STACK** - pushed on
   branch entry, popped at flush - and emit to the innermost sink if one is
   installed, else to the module-global observers as today. S4.1 specs this.
2. Even as a contextvar, a record can be emitted **off the awaiting task**, so it
   escapes the branch's contextvar context: `Clock.fire` (~2661), `Job`
   completion (`job.run ... done` ~2560), and `_revl_record_spawn` (~358) each
   call `_record` from a context that is not the branch task's. A timer or job
   that a branch *starts* can fire its record after the branch has flushed, on a
   different task, landing outside the buffer and in real-time order.

So C1 holds only under BOTH the contextvar sink-stack refactor AND a
**synchronicity restriction**: slice 2 parallelizes only emissions all of whose
audit records are emitted **synchronously within the awaiting task**. Emissions
whose record production is routed through a timer, a job completion, or a spawn
recorder are excluded from multi-emission groups (they degrade to sequential).
The alternative - capturing at the fiber level rather than the asyncio-task level
so off-task records are still attributed to the originating branch - is noted as
a later option; slice 2 takes the restriction because it is checkable from the
declaration surface, where "does this emission's host body route records off-task"
is not.

### 3.3 Teardown and recovery: EFFECT-equivalent, not byte-identical (revised)

This is the crux, and it is where the second adversarial review (2026-08-31)
overturned the prior claim. LIFO teardown (G7) and reconstructive recovery
(`recovery.py`) both key off the **acquisition order**: the order inverses /
compensations were registered onto the per-activation stack. On py this is the
yield order of disposers into `Frame`; `FaultProbe.accumulated` is precisely that
order, and R1/A8 hold iff `ran == reversed(accumulated)`. In the emitted body,
`lower` / `emit.py` place the compensation registration *immediately after* the
fire (`emit.py` ~1116):

```
# backends/python/emit.py, kind == "emit"
await <fire>
yield _revl_frame.compensation(lambda: <compensate>)   # registers onto the LIFO stack
```

The prior draft reduced everything to one invariant, plan-order registration
(P below), and concluded `accumulated` is **byte-identical** to sequential. That
conclusion is **wrong**, and withdrawn. Byte-identity of `accumulated` needs the
same *set* of registrations, and P defends only their *order*. Two paths change
the set:

- **Internal fault.** Group `[e1, e2, e3]`, `e2` raises. Sequential:
  `accumulated=[e1]` - e3 never runs. Parallel (S5) awaits the whole group and
  registers every branch that fired, so `accumulated=[e1, e3]`. The idempotent
  gate (S7-C2) makes e3's forward *delivery* a benign re-send but says nothing
  about e3's *compensation*: an idempotent "send welcome" can carry a real
  "send retraction" compensation, and parallel teardown would run it where
  sequential never registered it.
- **External A1 divert / cancellation.** A deadline expiry, a sibling-component
  fault, or an explicit cancel that lands at an `await` mid-run makes SEQUENTIAL
  execution SKIP every remaining emission (`replay.py:936` - "a close during an
  await skips the later steps"). Three 400ms emissions take >1s straight-line, so
  a mid-run divert is the NORMAL teardown trigger. The parallel shape collapses
  all three into one `await _revl_parallel(...)` that S5 refuses to abandon, so
  the divert is suppressed until the whole group fires: `accumulated=[e1, e2, e3]`
  vs sequential `[e1]`, plus G4 over-emission. The idempotent gate does not even
  engage - a divert is not a branch fault.

So byte-identical `accumulated` is **unachievable** for any group that can fault
or be diverted, which is essentially every useful group. The design does not
chase it. Two invariants survive, and together they give the corrected criterion:

> **P (plan-order registration).** Every compensation/inverse a parallel group
> *does* register is registered onto the activation's LIFO stack in the group's
> **sequential plan order**, regardless of completion order.

> **E (teardown-EFFECT equivalence).** After any teardown - clean commit,
> internal fault, or external divert - the world ends in the **same state** a
> sequential run would leave it in, even when the *set* of registered
> compensations differs from sequential's.

P is enforced by structure (S4): the runtime starts a group's host calls
concurrently, **awaits the group to quiescence**, then walks it in plan order
doing per member exactly the sequential post-fire work - bind the result, replay
the buffered audit, `yield` the compensation. Every *observable registration* is
single-threaded and in plan order, so `reversed(accumulated)` over whatever set
*did* register is still a correct LIFO order and `recovery.py` reconstructs it
newest-first as before. P alone no longer buys byte-identity; it buys a
well-ordered stack over the actually-registered set.

E is what makes the *different set* harmless, and it is bought by the **slice-1
parallelizable-set restriction** (S5, S8): a multi-emission group is formed only
from emissions whose **compensation is itself idempotent-or-absent** - not merely
whose forward delivery is idempotent. Under that restriction, running an extra
compensation (the fault case's e3, or a diverted member's) is a no-op-or-absent
on world state, so `[e1, e3]` and `[e1]` tear down to the same world, and a group
over-fired under divert tears down to the same world a skipped sequential tail
would. E holds by the restriction; P holds by structure; byte-identity of
`accumulated` is neither claimed nor needed.

The `commutative` recovery upgrade (Def. 39) composes without interaction: a
same-key `commutative` group's compensations may *additionally* be reordered at
teardown (Def. 39's existing payoff), but they are still *registered* in plan
order under P, so a non-commutative body's teardown effect is unchanged and a
commutative body's is exactly what Def. 39 already permits. Parallelism adds
nothing to the recovery contract beyond E's restriction on which groups form.

---

## 4. The runtime fan-out (py and ts)

The plan is consumed by the emitter, which renders a group of size > 1 as a
concurrent fire-then-join. The shape is identical on both tiers; only the
concurrency primitive differs.

### 4.1 Python (cordis-py, `backends/python`)

A group renders as: build one coroutine per member (the member's fire
expression, un-awaited), gather them under a **task-local audit sink** per
branch, await the gather, then in plan order bind results, flush each branch's
captured records to `_record`, and `yield` each compensation.

```
sketch  (emitted shape for a size-3 group; today's per-emit shape in SS3.3)

  _g = await _revl_parallel([
      _revl_branch(lambda: <fire e1>),
      _revl_branch(lambda: <fire e2>),
      _revl_branch(lambda: <fire e3>),
  ])                                   # concurrent; each branch buffers its records
  # --- join: everything below is single-threaded, in PLAN ORDER ---
  <bind e1 result>; _revl_flush(_g[0])
  yield _revl_frame.compensation(lambda: <compensate e1>)   # if e1 declared one
  <bind e2 result>; _revl_flush(_g[1])
  yield _revl_frame.compensation(lambda: <compensate e2>)
  <bind e3 result>; _revl_flush(_g[2])
  yield _revl_frame.compensation(lambda: <compensate e3>)
```

`_revl_parallel` is a thin runtime helper (`asyncio.gather` under the single
cordis event loop, no threads - the runtime is deterministic by construction,
`schedule.py`'s design decision 3). `_revl_branch` **pushes a record sink onto the
contextvar-backed sink stack** for the duration of the call so mid-call `_record`
events land in the branch buffer, not the real sink; `_revl_flush` pops the
branch sink and replays its buffer to the real `_record` in plan order. This is
the refactor HIGH-1 requires: `_record` (~420) must be changed to consult the
sink stack (innermost installed sink wins, else the module-global observers), and
only emissions whose records are produced **synchronously in the awaiting task**
may join a multi-emission group (timer/job/spawn-routed producers - `Clock.fire`
~2661, `Job` completion ~2560, `_revl_record_spawn` ~358 - are excluded, S3.2).
Because cordis-py is single-threaded cooperative, "concurrent" means the branches'
`await` points interleave on one loop - the latency win (three host round trips in
flight at once) with no data race on python state, and the atomicity the S2.3
interleave-safety theorem rests on.

**Divert handling (NEW CRITICAL).** `_revl_parallel` sits at a single `await`, so
a `GeneratorExit` / `CancelledError` arriving mid-group is the A1 divert path
(`replay.py:936`). Slice 1 does not make `_revl_parallel` abandon in-flight
branches; instead the checker only forms a multi-emission group from emissions
whose compensation is **idempotent-or-absent** (S3.3 invariant E, S5), so
over-firing the whole group under a divert and then running those compensations
leaves the same world state a skipped sequential tail would. A **divert-aware**
`_revl_parallel` - on cancel, propagate cancellation to un-started branches, await
only started ones, and register only members that actually fired - is a later
option; it makes the fired set nondeterministic, so slice 1 takes the
conservative restriction and documents the divert-aware runtime as future work.

A group of size 1 emits the **unchanged** today's shape (S3.3), so a body with
no provable parallelism is byte-identical output, and no size-1 emission is ever
subject to the E restriction (it is not a multi-emission group).

### 4.2 TypeScript (cordis, `backends/typescript`)

Identical structure with `Promise.all` and a per-branch record sink. The ts
`record(...)` sink (`runtime.ts`) is redirected to a branch-local array for the
branch's duration and concatenated in plan order at the join. cordis' disposer
protocol already awaits a returned promise, so the plan-order `yield` of each
inverse is unchanged.

### 4.3 What rejoin preserves

At the join the runtime restores, in plan order: (a) result bindings, (b) the
audit trace, (c) the compensation/inverse stack. Everything after the group -
the next group, or the sequence-breaking step that ended the run - sees a state
indistinguishable from sequential execution. Group `k+1` starts only after group
`k`'s join, so the groups themselves are a sequential spine; concurrency is
strictly *intra-group*.

---

## 5. G-invariant interaction

No G-invariant is weakened; several are the reason the design is shaped as it is.

- **G4 (emission upper bound).** The number of emissions the runtime fires must
  not exceed the declaration's *bound*. On the **success path** a parallel group
  fires exactly its members - same count as sequential, and always within the
  declared bound. On the **fault path** (S7-C2) and the **divert path** (S3.3,
  the NEW CRITICAL) concurrency can fire a member a sequential early-abort or a
  mid-run A1 divert would have skipped. This never exceeds the group's declared
  bound (every member was already counted toward it), so G4's *upper bound* is
  not violated; what changes is the *set actually fired* versus a sequential
  early exit. The fail-safe (S5 restriction, S3.3 invariant E) keeps that
  divergence teardown-EFFECT-harmless by forming multi-emission groups only from
  idempotent-forward, idempotent-or-absent-compensation members. G4 is never
  *relaxed*.
- **G5 (no emission in teardown).** Unchanged. Parallelism touches only the
  forward (Phase 0) firing of emissions; the teardown path (Phase 1 inverses,
  Phase 2 compensations) is never parallelized and never gains an emission.
  Compensations are registered, not run, at the join.
- **G7 (LIFO teardown).** The whole of SS3.3. Registration happens in plan
  order at the join, so `accumulated` is identical to sequential and
  `reversed(accumulated)` - the LIFO order - is unchanged. A fault in one branch
  must still unwind the whole activation newest-first; SS8-C2 shows the
  registered set on a fault is exactly the branches that fired, in plan order,
  so the unwind is well defined.
- **G8 (enumerable boundary surface).** The `parallel_plan` key is additive;
  `_boundary`'s emission set and capability map are unchanged, so `audit --diff`
  sees no drift from parallelization alone.

A fault in one parallel emission must still unwind correctly: the runtime awaits
the *whole* group (it never abandons an in-flight branch), collects every
branch's outcome, registers the fired branches' compensations in plan order, and
then re-raises the first fault so the activation's normal L-Raise teardown runs
over a correctly-ordered stack. The awaited-join is what makes a mid-group fault
safe against a *data race*: there is never a live concurrent branch racing the
teardown.

But the awaited-join is exactly what the NEW CRITICAL (S3.3) exposes on the
**divert** path: because the group is a single `await _revl_parallel(...)`, an A1
divert / cancel / deadline arriving mid-group cannot skip the group's tail the
way a sequential body would (`replay.py:936`); the whole group fires first, so
the fired set exceeds sequential's. This design does **not** resolve that by
byte-identity (impossible - see S3.3) but by teardown-EFFECT equivalence
(invariant E): the S5/S8 restriction admits into a multi-emission group only
members whose forward delivery is idempotent AND whose compensation is
idempotent-or-absent, so firing and then compensating the extra members leaves
the same world state a skipped sequential tail would. A divert-aware
`_revl_parallel` that abandons un-started branches is the alternative, noted as
future work in S4.1; slice 1 takes the conservative restriction. The prior draft
was silent on divert entirely; that silence was the core defect this revision
closes.

---

## 6. Silent, no new syntax, fail-safe by default

The checker derives the plan from declarations the author already wrote:
capability scopes (item 294) and `commutative` (Def. 39). There is no annotation
to add, no block to open, no unsafe flag. A body the author never thought about
parallelizing runs `~max` instead of `sum` the moment its emissions are provably
disjoint, and a body that isn't provably independent runs exactly as it does
today. The default when the proof is absent is **sequential**:

- unknown reach (`*`, first-class-surfaced dispatch) -> singleton groups (S7-C5);
- a first-class arrow call whose reach is `*` -> **barrier** (S2.1, HIGH-2): the
  straight-line run breaks across it, so no group spans the hidden crossing;
- same-token same-cone caps without `commutative` -> sequential;
- same-token sibling cones in slice 1 (before (D2) lands) -> sequential;
- a member that is faultable-non-idempotent, OR carries a non-idempotent
  compensation, OR routes audit records off the awaiting task -> excluded from a
  multi-emission group (the C2/E + synchronicity restriction), so it stays
  sequential and is never over-fired under a fault or an A1 divert;
- any sequence-breaking step (branch, loop, `let`-dependence, await) -> group
  boundary.

Every one of these is a *refusal to parallelize*, which can only ever make the
program slower, never wrong. The feature has no failure mode that widens
authority or changes results on the success path; its worst case is "no
speedup," which is the pre-259 status quo.

---

## 7. Adversarial self-review (mandatory)

Every prior design review found a CRITICAL. Two are found here; the first is the
headline.

### C1 (CRITICAL) - the audit surface races because host bodies record mid-call

**Attack.** "Same audit surface" reads as though the audit trace is produced at
the revl seam, where the runtime controls order. It is not. Host extern bodies
call the trace sink *themselves, mid-call*: on ts, `PoolHandle.query` runs
`record(\`${this.label}.query(${sql})\`)` inside the host method; on py the
`_record` observers fire from inside host builtins. Fan three disjoint emissions
out with a plain `gather`/`Promise.all` and those mid-call records interleave in
completion order, which varies run to run. The trace is nondeterministic, so
`audit_diff.py` sees drift and the exit criterion "byte-identical audit" fails on
the very first parallel group - the feature breaks its own contract.

**Mitigation.** Each branch runs under a **branch-local record sink** for the
duration of its host call (S4). Mid-call records are captured into a per-branch
buffer, not emitted. At the join the runtime concatenates the branches' buffers
**in plan order** and flushes them to the real sink. The observable trace is the
plan-order concatenation - byte-identical to sequential. This is why the runtime
fan-out is fire-buffer-join, not fire-and-forget.

*Refinement (HIGH-1, adversarial review 2026-08-31).* "Task-local" understated
the work. `_record` (`runtime.py:420`) reads module globals, not a task-local, so
the sink has to be built as a **contextvar-backed sink STACK** that `_record`
consults (S3.2, S4.1). And a record emitted **off the awaiting task** -
`Clock.fire` ~2661, `Job` completion ~2560, `_revl_record_spawn` ~358 - escapes
the branch context even with a contextvar, so slice 2 additionally restricts
multi-emission groups to emissions whose records are all produced **synchronously
in the awaiting task**. C1 holds only under the sink-stack refactor plus this
synchronicity restriction.

**Residual (OPEN).** Buffered replay makes the trace *look* sequential even when
the true host order was not. If two branches share a real external resource the
declarations did not name (attack C3) and the host actually served them in the
order B-then-A, the replayed trace still says A-then-B. The audit would then be
*self-consistent but untruthful* about host order. This is contained by C3's
rule (disjoint declared caps are trusted to mean disjoint host resources) and by
the `commutative` promise for same-key groups, but a lying declaration makes the
replayed audit lie in lockstep. Marked OPEN: the audit is faithful **relative to
the declaration being complete**, no stronger. The schedule tester (item 295)
is the falsifier - it already flags an "order-dependent final state" when a
concurrent interleaving diverges, and it should be extended to sweep the
parallel plan's groups.

### C2 (CRITICAL) - a fault OR a divert changes the fired-and-registered set

**Attack.** Sequential execution of `[e1; e2; e3]` where `e2` raises never runs
`e3`: the raise propagates and the remaining steps are skipped. A parallel group
fires all three concurrently, so `e3` hits the host *before* `e2`'s fault is
known. Emissions are irreversible one-way crossings (G5) - `e3` cannot be
un-sent. The **NEW CRITICAL** (adversarial review 2026-08-31) shows this is not
just a fault-path story: an external **A1 divert** (deadline, sibling fault,
explicit cancel) landing at an `await` mid-run makes a *sequential* body skip its
remaining emissions too (`replay.py:936`), and since three 400ms emissions take
>1s straight-line, a mid-run divert is the *normal* teardown trigger, not an edge
case. Either way the parallel body fires - and registers a compensation for - a
member the sequential body would have skipped. This (a) breaks byte-identical
`accumulated` (S3.3, claim withdrawn), (b) can run a real compensation sequential
never registered (an idempotent-forward "send welcome" can carry a real
"send retraction" compensation), and (c) is an extra crossing relative to a
sequential early exit.

**Mitigation / fail-safe.** The runtime *always* awaits the whole group (S5), so
the fired set is deterministic: all members of every group *entered*. The prior
draft's resolution 1 gated on `idempotent` **forward delivery** only - which is
insufficient, because it says nothing about the *compensation* that runs on
teardown. The corrected resolution:

  1. **Parallelize a multi-emission group only when every member's forward
     delivery is `idempotent` AND its compensation is idempotent-or-absent** (the
     `idempotent` delivery modifier, item 309, rides the same `emission`
     declaration; "compensation absent" is the common case and is trivially
     safe). Under this, over-firing e3 on a fault or a divert and then running its
     idempotent-or-absent compensation leaves the **same world state** as never
     firing it (S3.3 invariant E, teardown-EFFECT equivalence). A group with any
     member that is faultable-non-idempotent OR carries a non-idempotent
     compensation degrades to sequential. This is the criterion divert safety
     needs, not just forward idempotence.
  2. (Deferred.) A **divert-aware `_revl_parallel`** that, on cancel, propagates
     cancellation to un-started branches and registers only members that actually
     fired. This narrows the fired set toward sequential but makes it
     nondeterministic (which branches had started is a scheduling fact), so it is
     future work, not slice 1.

**Residual (partially OPEN).** Resolution 1 shrinks the parallelizable set
further than the prior draft implied (a member is admissible only if BOTH its
delivery and its compensation are benign to repeat). Whether an operation is
truly idempotent-or-absent on both faces is a host truth revl cannot fully
verify; slice 1 keys off the `idempotent` declaration plus a declared-absent /
declared-idempotent compensation, both checkable. Widening is future work, called
out so a later slice does not assume the success-path proof covers fault or
divert.

### C3 (HIGH/OPEN) - disjoint declared caps, shared hidden host resource

**Attack.** `send.email` and `metrics.write` have distinct tokens, so (D1)
proves them independent. But both host bodies happen to write the same log file,
or both draw from one shared connection pool, or both mutate a process global.
Concurrency then reorders a *real* observable effect that no declared capability
named - the disjointness proof was about the *declared* surface, and the host
lied about its footprint.

**Mitigation.** This is exactly the confinement contract the whole capability
system already rests on: an extern may only touch what its declared caps name;
an extern reaching an undeclared boundary is already a G4/G8 violation and an
`audit --diff` widening. Parallelism does not widen the trust boundary - it
*consumes* the same guarantee G4 already requires. If the declaration is
complete, disjoint caps mean disjoint host resources and the reorder is
unobservable.

**Residual (OPEN).** revl cannot statically verify an arbitrary `@py`/`@ts`
body's true resource footprint, so a mis-declared extern makes parallelism
unsound in the same way it already makes G4/audit unsound. This is the *same*
open trust surface, not a new one, but parallelism *acts on* it (it reorders,
where sequential merely mis-counted), so the blast radius is larger. The
falsifier is again item 295's schedule sweep over the plan. The 2026-08-31
adversarial review confirmed this residual is honestly disclosed and accepted:
slice-1's D1-only fail-safe is as safe as the capability system and no safer,
the same "declaration is complete" trust items G4/414/256 already rest on. No
wording change; it is called out here only to record that the second review
looked and agreed.

### C4 (MEDIUM/OPEN) - a `commutative` pair that is not commutative at the host

**Attack.** A same-key group is parallelized on the strength of the operation's
`commutative` declaration (S2.3). If the author declared `commutative` but the
host operation is order-dependent (two writes to the same key, last-writer-wins),
concurrent rejoin produces a state that depends on completion order - a different
result than either sequential order, and a nondeterministic one.

**Mitigation.** `commutative` is a promise checked at the *seam* (`adapt.py`'s
`commutative-mismatch`: a commutative requirement cannot be satisfied by a
non-commutative candidate) and versioned as a MAJOR-breaking flag (`version.py`).
Parallelism trusts it exactly as much as Def. 39's existing recovery-reorder
already does. A lying `commutative` was already unsound for recovery reordering
before item 259.

*Category-error refinement (MEDIUM/HIGH, adversarial review 2026-08-31).* C4 as
written catches only a **lying** `commutative`. It misses a distinct error for an
**honest** one: Def-39 `commutative` declares reorderability of teardown/RECOVERY,
which is not the same predicate as interleave-safety of concurrent FORWARD firing
(a non-atomic read-modify-write behind the same key can lose an update under
interleaving even when every serial order and every recovery order is fine). S2.3
is revised to confront this: rather than overload `commutative`, the design
states and defends the theorem "recovery-commutative implies
forward-interleave-safe UNDER the single-loop no-true-parallelism model," and
documents that same-key concurrency rests on there being no true parallelism (a
threaded runtime could not use this payoff). The rest, host truth of the flag,
remains C4's open residual below.

**Residual (OPEN).** The host truth of `commutative` is unverified, same as
`idempotent` and the disjoint-footprint assumption. Falsified by item 295's
"order-dependent final state" finding, which is precisely a mis-declared
`commutative` detector; the parallel plan should be one of the orderings it
sweeps.

### C5 (LOW, mitigated) - a `*`-widened or first-class emission

**Attack.** An unscoped bare `emission` (capabilities `["*"]`), or an emission
reached through a first-class dispatch the G4 fixed point marks `*`, has a
capability set that is not statically disjoint-checkable against anything.

**Mitigation (clean).** `*` is top of the partial order and `disjoint` returns
false for any pair touching `*` (SS2.2), so a `*` emission is never independent
of anything - not even another `*`. It is forced into a singleton group and runs
sequentially. This is fail-safe by construction and needs no special case beyond
the `"*" not in tokens(...)` guard. No residual.

---

## 8. Sliced implementation plan

Each slice lands and is verifiable alone. Slice 1 is checker-only and changes no
runtime behavior (it only *adds* an IR key), so it is the safe first landing.

### Slice 1 - partition derivation + plan on the IR (checker only, landable alone)

- **`src/revl/cap_order.py`**: add `disjoint(a, b)` and a helper
  `no_common_lower_bound(a, b)` on the existing order (D1 always on, D2 present
  but the caller gates it off in slice 1). Reuses `split_ceilings`, `_leq_path`,
  `covers`. Unit-tested against the `covers` table.
- **`src/revl/parallel.py`** (new): `parallel_plan(ir) -> {component -> [group]}`.
  Reads the emission sequence with the *same* three `walk_expr` arms
  `__main__._boundary` uses (factor the shared walk so the two cannot diverge,
  the S3 tie), applies `independent`/`reorderable`, runs the contiguous
  pairwise-compatible grouping (S2.4). Straight-line runs only; every
  sequence-breaking step is a barrier. **Adds the first-class `*` barrier
  (HIGH-2, S2.1):** the module must also consult the fn-aggregate
  `_emitting_capabilities` / `unknown_dispatch` surface and hard-break the run at
  any step whose callee reach is `*`, because the three-arm walk alone cannot see
  a first-class emitting arrow. This is a slice-1 checker responsibility, not a
  runtime one.
- **`src/revl/lower.py`**: attach `"parallel_plan"` to each body during lowering
  (or as a post-pass over the lowered IR), so the emitters and `audit --json`
  can read it. Additive: absent-key consumers are byte-identical.
- **`src/revl/__main__.py` / `audit_diff.py`**: surface the plan on
  `revl audit --json` next to `cardinality` (a per-component "parallel groups:
  [0] | [1,2,3] | [4]" render line), so the derivation is inspectable before any
  runtime consumes it. This is the slice-1 exit surface.
- **Exit (slice 1):** a golden test asserting the derived plan for a
  three-disjoint body is `[[0],[1,2,3],...]` collapsed correctly, a same-key
  non-commutative pair is two singleton groups, a same-key `commutative` pair is
  one group, a `*`/first-class-surfaced emission is always a singleton, AND an
  opaque first-class arrow call between two disjoint emissions is a **barrier**
  that keeps them in separate groups (the HIGH-2 case - the group must not span
  the hidden crossing). The exit criterion is the corrected one: this slice
  proves the *partition and barrier set*, not runtime equivalence, so there is no
  claim about `accumulated` here. No runtime change; `pytest tests/` and the
  backend goldens are byte-identical.

### Slice 2 - python runtime fan-out

- **`backends/python/runtime.py`**: the **contextvar record-sink refactor**
  (HIGH-1): change `_record` (~420) to consult a contextvar-backed sink STACK
  (innermost installed sink wins, else the module-global `_trace`/`_observers`),
  then add `_revl_parallel` (gather under the cordis loop), `_revl_branch` (push
  a branch sink onto the stack), `_revl_flush` (pop and plan-order replay). No
  threads. This is a runtime change with blast radius beyond item 259, so it
  lands here, not in checker-only slice 1.
- **`backends/python/emit.py`**: render a group of size > 1 as the
  fire-buffer-join shape (S4.1); size-1 groups emit today's byte-identical shape.
  Gate multi-emission groups on the corrected C2/E fail-safe: a member is
  admissible only if its forward delivery is `idempotent` AND its compensation is
  **idempotent-or-absent** AND its audit records are produced **synchronously in
  the awaiting task** (the synchronicity restriction excludes timer/job/spawn
  record routers). Anything else degrades to sequential.
- **Divert handling (NEW CRITICAL):** slice 2 is the conservative path - it does
  NOT make `_revl_parallel` abandon in-flight branches on an A1 divert; the E
  restriction above is what makes over-firing under a divert teardown-EFFECT
  harmless. The divert-aware `_revl_parallel` (propagate cancel to un-started
  branches, register only fired members) is explicitly deferred (Later / S10).
- **Exit (slice 2):** three disjoint emissions complete in `~max` not `sum` (a
  timing assertion with stubbed host latencies), byte-identical result and
  `_record` trace vs the sequential emit. The teardown assertion is the
  **corrected** one - NOT byte-identical `accumulated`, but **teardown-EFFECT
  equivalence** (invariant E): a fault-in-one-branch test and a **divert-mid-group
  test** each assert the world ends in the same state as sequential, with fired
  members compensated in plan order and a correct L-Raise unwind, even though the
  registered set differs from sequential's. A negative test: a group with a
  non-idempotent-compensation member, or an off-task record producer, is proven
  to stay sequential.

### Slice 3 - typescript runtime fan-out

- **`backends/typescript/emit.py` + `runtime.ts`**: the same shape with
  `Promise.all` and a branch-local `record` sink (SS4.2).
- **Exit (slice 3):** the roadmap exit test on ts - `~max` not `sum`,
  byte-identical result and `hostLog`, unchanged disposer order; the
  cross-tier `audit --diff` between a py run and a ts run of the same body is
  empty.

### Later - the (D2) cone refinement and the item 295 sweep

- Turn on (D2) so same-token sibling cones (`fs.write(path="/a")` vs
  `path="/b")`) parallelize; extend item 295's schedule tester to sweep the
  parallel plan's group orderings as a falsifier for C3/C4.

---

## 9. Exit test (the roadmap's, restated)

A body with three disjoint-capability emissions completes in `~max(latencies)`
not `sum(latencies)`, with byte-identical results and byte-identical audit
surface, on both py and ts. A non-commutative same-key pair is proven to stay
ordered (two singleton groups; no concurrency).

**Corrected teardown criterion (adversarial review 2026-08-31).** The prior
exit test demanded byte-identical `FaultProbe.accumulated`. That is withdrawn: on
a fault or an A1 divert the parallel body's *registered set* legitimately differs
from sequential's (S3.3), so byte-identity of `accumulated` is unachievable for
any group that can fault or be diverted. The exit criterion is instead
**teardown-EFFECT equivalence** (invariant E):

- On the **clean-commit path**, `accumulated` and the disposer order are in fact
  byte-identical to sequential (no set difference arises), so the strong check
  still applies there.
- On the **fault path** (a fault in one parallel branch) and the **divert path**
  (a deadline / sibling-fault / cancel landing mid-group), the world must end in
  the **same state** as a sequential run, with every member that fired
  compensated in plan order and a correct L-Raise unwind, even though the
  registered set differs from sequential's. The divert case is an explicit exit
  test, not an afterthought, because a straight-line run of three ~400ms
  emissions makes a mid-run divert the normal teardown trigger.

Both faces are guaranteed by forming multi-emission groups only from members with
idempotent forward delivery and idempotent-or-absent compensation (S5, S8).

---

## 10. Open questions

1. **Beyond straight-line runs.** Slice 1 partitions only contiguous emission
   runs. Emissions in disjoint `if` arms, or across a `let` that no emission
   reads, are independent too but are not grouped. A control-flow-aware
   partition is a documented follow-on.
2. **The C2 boundary.** A multi-emission group forms only when every member has
   idempotent forward delivery AND idempotent-or-absent compensation AND emits
   its audit records synchronously in the awaiting task. Widening any of these
   three (to admit the fault/divert emission-set change for a non-idempotent
   member, or to capture off-task records at the fiber level) needs a
   per-operation proof or a policy; deferred.
3. **Divert-aware `_revl_parallel`.** Slice 1/2 take the conservative path: the
   E restriction makes over-firing under an A1 divert teardown-EFFECT harmless,
   at the cost of a smaller parallelizable set. A later `_revl_parallel` could
   instead, on `GeneratorExit`/`CancelledError`, propagate cancellation to
   un-started branches, await only started ones, and register only members that
   actually fired - narrowing the fired set toward sequential. It is deferred
   because the fired set then becomes nondeterministic (which branches had
   started is a scheduling fact), which is a harder property to test and to
   reconcile with a byte-identical audit than the conservative restriction.
4. **The (D2) same-token cone test** depends on the item 294 slice-2 runtime
   cone semantics settling; gated off until then.
5. **Item 295 integration.** The schedule tester should treat the parallel plan
   as one of the orderings it sweeps, turning C3/C4's OPEN residuals into
   falsifiable properties rather than trusted declarations. This is also the
   natural falsifier for the S2.3 single-loop interleave-safety theorem: run the
   plan under a hypothetical preemptive schedule and confirm nothing in the
   slice-1/2 target tiers relies on true parallelism.
