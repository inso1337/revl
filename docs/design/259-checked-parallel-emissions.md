# Checked parallel emissions - the latency answer, derived from declarations (item 259)

**Status: design, not implemented.** This document specifies how the checker
derives a parallelizable *partition* of a body's emission sequence from the
capability declarations already present, and how the py and ts runtimes fan
each parallel group out concurrently while producing byte-identical results,
a byte-identical audit surface, and an unchanged LIFO teardown. There is no new
syntax: the only declaration involved is `commutative` (Def. 39), which today
upgrades recovery ordering and here also earns an execution payoff. Anything not
provably independent stays sequential, silently.

The headline finding from the adversarial self-review (SS8) is stated up front
because it constrains every other section: **the audit surface is not produced
only at the revl seam - host extern bodies emit their own trace records
mid-call (`record(...)` inside `PoolHandle.query` on ts, `_record` observers on
py), so naive concurrency interleaves those records nondeterministically and the
"same audit surface" exit criterion fails on the first parallel group.** The
design's answer is a per-branch audit capture that replays in sequential plan
order at the join (SS4, SS8-C1). Read SS8 before SS4.

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
and loops are a documented follow-on (SS7, SS10).

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
rejoin in any completion order, because the author has declared that the final
state does not depend on the order the crossings land. A same-key run where
either side is **not** `commutative` stays strictly sequential - proven ordered,
which is the exit test's second half.

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
the headline finding (SS8-C1): host extern bodies write their own records
*mid-call*, so concurrency interleaves them. The design's answer is that each
parallel branch runs with a **task-local record sink**; at the join the runtime
concatenates the branches' captured records **in plan order** and only then
emits them to the real `_record`/`hostLog`. The observable trace is therefore
the plan-order concatenation - byte-identical to sequential - even though the
host calls overlapped in wall-clock. SS4 specifies the sink; SS8-C1 states the
residual risk (a trace that *encodes* a true cross-branch host order the replay
now hides).

### 3.3 Teardown and recovery are UNCHANGED (the load-bearing argument)

This is the crux. LIFO teardown (G7) and reconstructive recovery
(`recovery.py`) both key off one thing: the **acquisition order**, i.e. the
order in which inverses/compensations were registered onto the per-activation
stack. On py this is the yield order of disposers into `Frame` -
`FaultProbe.accumulated` is precisely that order, and R1/A8 hold iff
`ran == reversed(accumulated)`. In the emitted body, `lower`/`emit.py` place the
compensation registration *immediately after* the fire:

```
# backends/python/emit.py, kind == "emit"
await <fire>
yield _revl_frame.compensation(lambda: <compensate>)   # registers onto the LIFO stack
```

The teardown order is thus a pure function of the **registration order**, which
today equals source order. The entire soundness of unchanged teardown reduces to
one invariant:

> **P (plan-order registration).** Every compensation/inverse a parallel group
> produces is registered onto the activation's LIFO stack in the group's
> **sequential plan order**, regardless of the order the concurrent host calls
> completed in.

The runtime enforces P by structure (SS4): it starts all host calls in a group
concurrently, **awaits the whole group to quiescence**, and *then* walks the
group in plan order performing, per member, exactly the post-fire work the
sequential emitter would have done - bind the result, replay the buffered audit
records, and `yield` the compensation. The concurrency lives entirely *inside*
the fire-and-await; every *observable registration* happens single-threaded, in
plan order, after the join. Because `accumulated` is rebuilt in plan order, it is
identical to the sequential `accumulated`, so `reversed(accumulated)` - the LIFO
teardown, and the newest-first order `recovery.py` reconstructs - is identical
too. Concurrency cannot change the observable order for teardown or recovery
because concurrency never touches the registration path.

This is why the roadmap can say "acquisition order is recorded as the sequential
order of the plan, so recovery is unchanged": the plan *defines* acquisition
order, and the plan is the sequential order. Overlapping *execution* is
invisible to the stack.

The `commutative` recovery upgrade (Def. 39) composes without interaction: a
same-key `commutative` group's compensations may *additionally* be reordered at
teardown (that is Def. 39's existing payoff), but they are still *registered* in
plan order, so a non-commutative body's teardown is bit-for-bit unchanged and a
commutative body's is exactly what Def. 39 already permits. Parallelism adds
nothing to the recovery contract.

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
`schedule.py`'s design decision 3). `_revl_branch` installs a task-local record
buffer for the duration of the call so mid-call `_record` events are captured,
not emitted; `_revl_flush` replays a branch's buffer to the real `_record` in
plan order. Because cordis-py is single-threaded cooperative, "concurrent" means
the branches' `await` points interleave on one loop - which is exactly where the
latency win lives (three host round trips in flight at once) with no data race on
python state.

A group of size 1 emits the **unchanged** today's shape (SS3.3), so a body with
no provable parallelism is byte-identical output.

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
  not exceed the declaration's bound. On the **success path** a parallel group
  fires exactly its members - same count as sequential. On the **fault path**
  concurrency can over-fire (SS8-C2): a member fires that a sequential
  early-abort would have skipped. That is a G4 concern and is the second
  critical finding; the fail-safe resolution (SS8-C2) either keeps the count
  identical by construction or refuses to parallelize. G4 is never *relaxed* to
  accommodate parallelism.
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
branch's outcome, registers the successful branches' compensations in plan
order, and then re-raises the first fault so the activation's normal L-Raise
teardown runs over a correctly-ordered stack. The awaited-join is what makes a
mid-group fault safe: there is never a live concurrent branch racing the
teardown.

---

## 6. Silent, no new syntax, fail-safe by default

The checker derives the plan from declarations the author already wrote:
capability scopes (item 294) and `commutative` (Def. 39). There is no annotation
to add, no block to open, no unsafe flag. A body the author never thought about
parallelizing runs `~max` instead of `sum` the moment its emissions are provably
disjoint, and a body that isn't provably independent runs exactly as it does
today. The default when the proof is absent is **sequential**:

- unknown reach (`*`, first-class dispatch) -> singleton groups (SS8 attack 5);
- same-token same-cone caps without `commutative` -> sequential;
- same-token sibling cones in slice 1 (before (D2) lands) -> sequential;
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

**Mitigation.** Each branch runs under a **task-local record sink** for the
duration of its host call (SS4). Mid-call records are captured into a per-branch
buffer, not emitted. At the join the runtime concatenates the branches' buffers
**in plan order** and flushes them to the real sink. The observable trace is the
plan-order concatenation - byte-identical to sequential. This is why the runtime
fan-out is fire-buffer-join, not fire-and-forget.

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

### C2 (CRITICAL) - a fault in one branch over-emits relative to sequential

**Attack.** Sequential execution of `[e1; e2; e3]` where `e2` raises never runs
`e3`: the raise propagates and the remaining body steps are skipped. A parallel
group fires all three concurrently, so `e3` hits the host *before* `e2`'s fault
is known. Emissions are irreversible one-way crossings (G5) - `e3` cannot be
un-sent. So on the fault path the parallel body performs an emission the
sequential body never would, which (a) breaks byte-identical results/audit on the
fault path and (b) is an extra crossing G4 bounds. A group is *independent*, but
independence does not prevent this: it is a control-flow difference (early
abort), not a data hazard.

**Mitigation / fail-safe.** Slice 1 makes the count identical by construction:
the runtime *always* awaits the whole group (it never early-aborts a group
mid-flight, SS5), so the "emissions fired" set is deterministic - all members of
every group that was *entered*. The remaining divergence is only against a
hypothetical sequential early-abort. Two honest resolutions, and slice 1 takes
the first:

  1. **Parallelize a multi-emission group only when its members are declared
     non-faulting or `idempotent`** (the `idempotent` delivery modifier, item
     309, already rides the same `emission` declaration). An idempotent
     over-emission is a documented no-op re-delivery, so firing `e3` after `e2`
     faults is within contract, and each fired member is separately compensated
     in plan order at the join (SS3.3), so teardown is well defined. A group with
     any *faultable, non-idempotent* member degrades to sequential.
  2. (Deferred.) Admit the fault-path emission-set change as a documented
     semantic for independent emissions and prove it acceptable per operation.

**Residual (partially OPEN).** Resolution 1 shrinks the parallelizable set (many
useful emissions are faultable). Whether an operation is "non-faulting" is itself
a host truth revl cannot fully verify; slice 1 keys off the `idempotent`
declaration only, which is checkable. Widening beyond idempotent is future work
and is called out so a later slice does not silently assume the success-path
proof covers the fault path.

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
falsifier is again item 295's schedule sweep over the plan.

### C4 (MEDIUM/OPEN) - a `commutative` pair that is not commutative at the host

**Attack.** A same-key group is parallelized on the strength of the operation's
`commutative` declaration (SS2.3). If the author declared `commutative` but the
host operation is order-dependent (two writes to the same key where last-writer-
wins), concurrent rejoin produces a state that depends on completion order - a
different result than either sequential order, and a nondeterministic one.

**Mitigation.** `commutative` is a promise checked at the *seam*
(`adapt.py`'s `commutative-mismatch`: a commutative requirement cannot be
satisfied by a non-commutative candidate) and versioned as a MAJOR-breaking flag
(`version.py`). Parallelism trusts it exactly as much as Def. 39's existing
recovery-reorder already does - this item does not widen the trust, it extends
the *same* declaration to execution. A lying `commutative` was already unsound
for recovery reordering before item 259.

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
  the SS5 tie), applies `independent`/`reorderable`, runs the contiguous
  pairwise-compatible grouping (SS2.4). Straight-line runs only; every
  sequence-breaking step is a barrier.
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
  one group, and a `*`/first-class emission is always a singleton. No runtime
  change; `pytest tests/` and the backend goldens are byte-identical.

### Slice 2 - python runtime fan-out

- **`backends/python/runtime.py`**: add `_revl_parallel` (gather under the cordis
  loop), `_revl_branch` (task-local record sink), `_revl_flush` (plan-order
  replay). No threads.
- **`backends/python/emit.py`**: render a group of size > 1 as the
  fire-buffer-join shape (SS4.1); size-1 groups emit today's byte-identical
  shape. Gate multi-emission groups on the C2 fail-safe (idempotent/non-faulting
  members only) in this slice.
- **Exit (slice 2):** the roadmap exit test on py - three disjoint emissions
  complete in `~max` not `sum` (a timing assertion with stubbed host latencies),
  byte-identical result and `_record` trace vs the sequential emit, and a
  `FaultProbe` assertion that `accumulated` (hence the LIFO teardown) is
  identical to sequential. A fault-in-one-branch test asserting plan-order
  compensation registration and a correct L-Raise unwind (C2).

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
ordered (two singleton groups; no concurrency). The teardown/recovery order
(`FaultProbe.accumulated` on py, disposer order on ts) is byte-identical to
sequential execution in every case, including a fault in one parallel branch.

---

## 10. Open questions

1. **Beyond straight-line runs.** Slice 1 partitions only contiguous emission
   runs. Emissions in disjoint `if` arms, or across a `let` that no emission
   reads, are independent too but are not grouped. A control-flow-aware
   partition is a documented follow-on.
2. **The C2 boundary.** Slice 1 parallelizes multi-emission groups only when
   members are `idempotent`/non-faulting. Widening to admit the fault-path
   emission-set change for independent emissions needs a per-operation proof or
   a policy; deferred.
3. **The (D2) same-token cone test** depends on the item 294 slice-2 runtime
   cone semantics settling; gated off until then.
4. **Item 295 integration.** The schedule tester should treat the parallel plan
   as one of the orderings it sweeps, turning C3/C4's OPEN residuals into
   falsifiable properties rather than trusted declarations.
