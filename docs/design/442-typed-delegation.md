# 442: typed delegation, or why delegation should not become a verb

Design note for roadmap item 442. Design only: no compiler change, no `src/`
change, nothing implemented. Companion docs:
[294-parameterized-capabilities.md](294-parameterized-capabilities.md),
[308-effect-ownership-modes.md](308-effect-ownership-modes.md),
[246-auto-approve.md](246-auto-approve.md),
[337-polyglot-admission-mesh.md](337-polyglot-admission-mesh.md),
[426-composition-layers.md](426-composition-layers.md),
[teardown-contract.md](teardown-contract.md).

The item asks for a way for one component to hand a scoped, revocable
reference to one of its held provisions to a component that is not its child,
with the handoff itself checked. It proposes `delegate` as a language-level
crossing with a gate-minted receipt.

**The recommendation is that `delegate` should not become a verb.** The item's
own title is also its resolution. Every mechanism the item asks for is already
built and shipped, in three separate places, and the whole feature is: put a
type on the handle that item 294's lease already mints, and admit that type in
a service method's parameter list. That is a small feature, and this note
argues it is the correct one.

---

## 0. The decisions, in one table

| # | Question | Decision | Section |
|---|---|---|---|
| Q1 | Revocation versus G7 | **Revocation is forward-only and monotone.** It removes the right to incur new obligations, never the duty to discharge accumulated ones. The conflict is made inexpressible by rule D2, not arbitrated at runtime. | §3 |
| Q1b | Does 426 R1 block this? | **No.** 442 routes around it: revocation retires a ledger row, it never withdraws a provision, disposes a fiber, or re-resolves a consumer. | §3.5 |
| Q2 | Transfer or share? | **Share.** The provider table is never touched, so G2's `(key, realm)` unit is unchanged. In 308's vocabulary the delegated reference is `borrowed`, never `transfer`. | §6.1 |
| Q3 | Depth, and does the receipt chain or flatten? | **Chain, and bound it at 1 in v1.** A `Delegate[S]` value may not be passed across a second crossing. The receipt is chain-shaped from day one so depth is additive later; the bound is the finite service-signature DAG, never a counter. | §6.2 |
| Q4 | How is the handoff checked? | The delegated reference is a **typed value in a declared service signature**, so the handoff is an ordinary type check plus the 308 borrow walk. No new wiring plane, no new reachability analysis. | §4 |
| Q5 | Is this 294's lease under another name? | **Substantially yes, and that is the finding.** The runtime is 294's, entirely. What 294 cannot express is a second party, and the second party costs one type constructor and one parameter position. | §7 |
| Q6 | New verb? | **No.** `effect lease` extended to name a wiring key, yielding a typed handle. One runtime, one ledger, one revocation path. | §5 |
| Q7 | Does 442 add reach? | **No. It subtracts reach, dynamically, under a static ceiling that G8 still enumerates.** This is the honest value statement and §8 holds it to it. | §8.1 |

---

## 1. What already exists, measured

Four mechanisms in the shipped tree cover most of what the item asks for. They
are listed with their seams because the design is mostly a wiring-together, and
a wiring-together is only honest if the parts are named exactly.

### 1.1 Attenuation exists, and its domain is lineage only

`_check_spawn_attenuation` (`src/revl/lower.py:10671` and its neighbourhood)
enforces `reach(child) subset-of held(parent)` on every activation-body spawn,
comparing structured `(T, P)` capabilities through `cap_order.covers`, with the
key-to-token bridge `_cap_keyed` reading the DECLARED capability token on both
sides (item 421 F1's fix). Budget ceilings attenuate too: the hint the roadmap
item quotes, a ceiling "attenuates on delegation: the child's ceiling must be
<= the parent's", is the ceiling half of the same check.

The domain of that check is the **spawn lineage**, and only that. There is no
comparable relation between two components that are peers in a composition,
because there is no event between peers for it to attach to. `spawn` is the
only construct that creates a new authority holder from an existing one.

### 1.2 Bounded, revocable, expiring authority exists and is shipped

Item 294 Slice 2 landed the lease runtime on top of the 344/379 grant ledger:

```
let l = effect lease fs.write(path="/tmp/job-42") ttl 10m uses 20
        undo l.revoke()
```

lowering (`_lower_lease_step`, `src/revl/lower.py:9581`) to a `let-effect` step
with two dedicated IR nodes, `lease-acquire` and `lease-revoke`. The grant row
carries `{capability, candidateHash, component, session, grantedAt, expiresAt,
remainingUses, consumed, revoked}`; liveness is checked **at the crossing, not
at the mint** (`_live_grant_for`); uses are consumed before the fire with WAL
ordering (`_consume_grant`); the mint is class-(c) ticket-gated so a program
cannot mint its own consent; and the disposer must be exactly `<bind>.revoke()`,
the own-requestId revoke, which is the one exempt disposer.

Everything the item asks a receipt to carry, the grant row already carries: a
hash over the covered surface (`candidateHash` over the reach closure), an
expiry, a uses bound, a revocation hook, and a generation binding that lapses
the grant automatically when the holder is swapped.

What the lease cannot do is name a **subject**. Its holder and its subject are
the same scope. `effect lease` narrows what the acquiring frame may already do;
it has no way to address a second component, because the capability algebra's
only transfer relation is spawn lineage (§1.1) and the only nameability
relation is `requires`, which is static wiring.

### 1.3 A typed, unforgeable authority value exists

Item 246's `Approval[C]` is the precedent for putting authority in the type
system: a value with no constructor (`await approval[C]` is its only producer,
so it cannot be forged in-language), threaded to a crossing by an explicit
`with` clause so the obligation is a type check rather than a new reachability
analysis, and non-persistent (refused in a snapshot shape, a handoff shape, or
a spawn config, invariant 5).

And 246 left exactly this item open, in its own words. Open question 3:

> A workflow where component X legitimately brokers an approval for component Y
> is not supported; whether it should be a first-class delegation (with its own
> binding) or two approvals is open, and the confused-deputy default is the safe
> one.

with the mechanical half named right after it: the `with` edge cannot thread
across an interface "unless the service method's signature itself carries
`Approval[C]` as a parameter type, which v1 does not add."

**442 is 246 open question 3, generalized from a capability token to a
provision.** That is not a coincidence to note in passing; it is the design.
The answer 246 could not give is the one this note gives in §4: the parameter
type.

### 1.4 A borrow discipline exists, and it is exactly the scoping rule

Item 308 landed owned and borrowed modes over the teardown accumulator. Rule
B1's seven clauses are the complete list of ways a value can outlive the scope
that received it: activation state, closure capture, non-owner return, a
carrier record or collection, an undo / witnessed-argument / compensate
position, spawn config, and a handoff type. Passing a borrow further DOWN a
call chain is admitted; the callee's parameter is another borrow.

A delegated reference is a borrow by every definition 308 uses. The scoping
half of 442 therefore needs no new analysis, only a new tainted type for the
existing walk to visit.

### 1.5 Seam judgement exists, and it forbids bearer receipts

Item 337's amendment, "a selector is not an authorization", is the rule that
governs 442 the moment a delegation crosses a process. The wire carries a
SELECTOR; the receiver derives every gate input from state it holds
independently and re-decides; a reference the receiver's own state does not
admit is refused, fail-closed. Applied here: a receipt on the wire is a
lookup key into the receiver's own ledger, never a bearer token whose contents
are the authorization. §9 states the consequence.

---

## 2. The gap, stated precisely

Component `A` holds `fs`, a broad filesystem provision, by requiring it.
Component `B` is a worker `A` calls. `B` needs to write one directory, for the
duration of one call, and needs it only because `A` asked it to.

Today `A` has three options and all three are bad in a different way:

1. **Widen `B`'s `requires`.** `B` declares `requires fs: FileSystem`, and now
   holds the whole provision for its entire activation lifetime, on every call,
   including calls `A` did not make. Permanent, unvalued, unbounded.
2. **Route every call through `A`.** Correct, and it eliminates the point: `A`
   becomes a forwarding surface for every operation `B` might want, and every
   new operation is a new method on `A`.
3. **Spawn `B` as a child with attenuated capabilities.** Correct and bounded,
   but it changes the topology: `B` is now an instance `A` owns and disposes
   (`spawn C ... undo s.dispose()`), not a peer in the composition. If `B` must
   be a shared, singly-provided peer, this is not available.

The missing thing is narrow and can be stated in one sentence:

> There is no way to give a second party a bounded, temporary, valued view of
> an authority you hold, for the duration of a call you are making into it.

Note what is NOT missing. `B` reaching `fs` at all is not a capability gap:
option 1 is admitted today and G3 permits it whenever the topology is acyclic.
So 442 is not about making a new reach possible. It is about making an existing
reach **smaller in time and in value**. §8.1 holds the design to that claim.

---

## 3. Q1: revocation versus G7

This is the crux and the item is right to name it. The rest of the design is
assembly; this section is a decision.

### 3.1 The question, stated so it can be answered

`A` mints a delegation on `fs` and hands it to `B` inside a call. `B` is inside
an activation. `B` performs an effect through the delegated reference; by G4
that effect carries an inverse, and by G7 the inverse is registered on `B`'s
per-activation LIFO stack and will replay at teardown, exactly once, commit or
abort alike. Before that teardown runs, `A` revokes the delegation.

Three outcomes are conceivable and two of them are unacceptable:

- **Revocation wins.** The revocation cancels `B`'s accumulated inverses. G7 is
  broken: an accumulated effect no longer has a replayed inverse, and the
  teardown contract's "LIFO-complete over the accumulated effects" is false.
- **G7 wins.** Teardown replays the inverses under a revoked authority.
  Revocation is a lie: authority that keeps producing host effects after it was
  retired is not revoked, and the receipt an operator reads is wrong.
- **The state is unreachable.** Neither guarantee is weakened because the
  configuration that would force a choice cannot be written.

**The decision is the third**, and it is reached by two rules, one semantic and
one static.

### 3.2 D1: revocation is forward-only and monotone

**Revocation is a monotone predicate on AUTHORIZATION, evaluated at the forward
crossing.** It is never a teardown event. It does not withdraw a provision, does
not dispose a fiber, does not re-resolve a consumer, and does not touch the
provider table. It removes the right to **incur** new obligations. It never
removes the duty to **discharge** accumulated ones. Once retired, a grant never
becomes live again; a later handover mints a NEW grant with a new receipt.

The asymmetry that makes this coherent, stated once because everything else
follows from it:

> Authority is required to incur an obligation, not to discharge one.
> Discharging is the system keeping a promise it made while it held the
> authority, and a promise made under authority held at the time does not become
> unauthorized when the authority lapses.

This is not a new rule invented for 442. It is the rule the shipped system
already runs:

- item 246 consumes the approval **at the crossing** and never re-checks it at
  teardown; an approved emission's compensation is not re-approved;
- item 243's `transactional` entry replays a **host-local** inverse whose
  authority was settled on the `Ok` branch, and the teardown contract requires
  it not to emit (G5);
- item 294 checks `expiresAt` at the crossing, not at the mint, precisely so
  that authority is judged where it is spent.

D1 makes 442 consistent with all three rather than introducing a fourth
policy.

### 3.3 D2: a delegated reference may not appear in any inverse position

D1 alone is not enough, because an inverse could *physically* need the
delegated provision: `undo r.rollback()` where `r` is the delegated reference
is an inverse whose replay is a forward crossing through a possibly-dead
delegation. D1 would then be doing arithmetic on a contradiction.

So the configuration is refused statically. A `Delegate[S]` value is
resource-tainted, its mode is `borrowed` in the receiving scope, and 308's B1
applies verbatim, with clause 5 load-bearing:

> A borrowed value may not appear in an undo expression, in a witnessed
> effect's argument list, or in a compensate expression.

Consequence, and it is the whole answer: **no entry on any LIFO stack anywhere
holds a delegated reference.** Every accumulated inverse under a delegation is
either host-local to the borrower, or routed through the borrower's own
declared `requires`. Neither depends on the delegation being live.

```
error: worker.rvl:31: delegated reference `fs: Delegate[FileSystem]` cannot
  appear in an `undo` expression. An undo lives on this activation's LIFO stack
  until teardown (G7) and would replay through a delegation that may already be
  revoked. A delegated reference authorizes forward crossings only.
  hint: register the inverse against a provision this component itself
  requires, or perform the reversible half through the delegator's own service.
```

Clause 5's existing exemption (the acquiring binding's own `undo`) is exactly
`undo d.revoke()` on the delegator's side, which is the mint's own bracket, not
an escape.

### 3.4 The four cases, worked

| case | what happens | which rule |
|---|---|---|
| Revoked before any crossing | No obligations exist. The next crossing refuses at the seam, naming the retired grant. | D1 |
| Revoked between crossings, mid-bracket | Prior inverses stand and replay LIFO at teardown. The next crossing refuses. **This is the case the item names, and it is not a conflict.** | D1 + D2 |
| Revoked while a crossing is in flight | The crossing is decided once, at its liveness check, before the fire, and consume-before-fire makes the decision durable. Revocation is a fence on the crossing SEQUENCE, not on wall-clock time: eventual with respect to time, exact with respect to crossings. This is the same honesty 294 states for realm revocation ("eventual over the walk, not a fence"). | D1 |
| The delegator is withdrawn entirely (unload, swap, rollback) | Every grant it minted lapses by generation liveness (`candidateHash`) with no bookkeeping, so the delegation auto-revokes. `B`'s accumulated inverses still do not touch it (D2). | shipped 294/344 |

The last row is worth dwelling on: it is free, it is already implemented, and
it is the correct semantics. A swap of the delegator invalidates every
reference it handed out, without a single line of delegation-specific
bookkeeping, because the grant is bound to the delegator's candidate hash.

### 3.5 Why 426 R1 does not block this

The item files Q1 as blocked on 426 R1, the residual for incremental activation
of `replace` and `remove`. 426 §5.2 states why that residual is blocked, and the
reason is specific: withdrawing a wired row requires disposing the withdrawn
component's fiber, replaying its accumulated teardown in the correct LIFO
position, and re-resolving every consumer bound to its key. That is a fiber
lifecycle project and it does put G7 at risk.

**Under D1, revocation does none of those three things.** It retires a row in
the grant ledger. No fiber is disposed, no teardown is replayed out of order, no
consumer is re-resolved, no provider table entry changes, and the row `A`
occupies in the composition is untouched. `A` is still the provider of
everything it provided a microsecond earlier; it is simply no longer lending it
to `B`.

The item's framing of Q1 as "a withdrawal under a consumer that did not declare
it" is what makes it look blocked, and that framing is what this note rejects.
A revoked delegation is not a withdrawal. **Item 442 is not blocked on 426 R1.**

The dependency that IS real runs the other way and is much weaker: 426's row
table (S1) is what would eventually let a delegation ceiling be printed per row
in the authority panel (§8.2). That is presentation, not admission.

---

## 4. Q4: how the handoff is checked

revl's guarantees are structural and checked. A delegation the checker cannot
see would be out of character, and the item is right to insist on it. The move
that makes it checkable is to refuse to invent a new plane for it.

### 4.1 The delegation is a value in a declared service signature

A service method declares that it accepts a delegated reference:

```revl sketch
service Worker {
  emission fn run(job: Str, fs: Delegate[FileSystem]) -> Int
}

component Worker0 provides worker: Worker {
  // `fs` is a declared PARAMETER, so G1 admits the name. `Worker0` gains no
  // `requires` key, no provider binding, and no way to originate a call on
  // `fs`: it can only use a reference handed to it, for this call.
  provide worker { fn run(job, fs) = fs.write(job, ...) }
}
```

Everything follows from that one line:

- **G1 is untouched.** `B`'s provide method names `fs` because `fs` is a
  declared parameter, which G1 has always admitted. `B` gains no `requires` key,
  no wiring binding, and no ability to originate a call on `fs`: it can only use
  a reference someone chose to hand it, during a call it was already serving.
- **G2 is untouched.** No provider table entry is created, moved, or shadowed.
  The delegated provision keeps exactly one provider, which is whoever provided
  it before (§6.1).
- **The handoff is a type check.** The obligation is ordinary dataflow, which is
  246's own argument for the `with` clause: "the explicit dataflow that turns
  'unreachable without approval' into a type check instead of a new reachability
  analysis."
- **It is header-only computable.** Service declarations and component headers
  are enough to build the whole delegation graph, so the 426 §1.3 cheapness
  property carries over: `revl audit` can render the delegation ceiling without
  lowering a body.

### 4.2 The augmented graph must stay acyclic, and this is a real refusal

A delegation adds an authority edge the static graph did not have: `B` can now
reach the provider of `fs` while `A` is inside a call to `B`. If that provider
IS `A`, the augmented graph has a cycle, `A -> B -> A`, and `A`'s provide method
runs reentrantly while `A`'s own activation body is suspended mid-effect. G3
exists to refuse exactly that shape statically.

**Rule D3.** For each delegation site where component `A` passes a
`Delegate[S]` minted on key `k` to a call on its required key `b`, add the edge
`provider_of(b) -> provider_of(k)` and run G3's existing cycle check over the
augmented graph.

The consequence is worth stating loudly because it constrains the item's own
wording. The item asks for a component to hand a reference "to one of its own
**provisions**". Read as "a key `A` itself provides", handed to a component `A`
calls, that is a cycle and D3 refuses it. This is not a new restriction invented
here: the item's own named alternative, widening `B`'s `requires`, produces the
identical cycle and G3 refuses it today. **Delegation must not become a G3
bypass.**

So the admitted and flagship shape is: `A` delegates a key from its **held
cone** (its `requires` keys plus its own emission surface, `_held_capabilities`)
to a component it calls, where the augmented graph stays acyclic. Delegating a
key `A` requires is the ordinary case and is acyclic whenever the topology
already was.

```
error: app.rvl:22: `Ingest` cannot delegate `db` to `Worker`: `Worker` would
  reach `Ingest`'s own provision while `Ingest` is inside a call to `Worker`
  (G3, augmented with delegation edges).
    Ingest  app.rvl:14  requires worker: Worker
    Ingest  app.rvl:14  provides db: Database
  hint: a delegation may only pass authority OUTWARD, from a component's held
  cone. Move the provision to a third component, or have `Worker` require `db`
  directly if it needs it unconditionally.
```

### 4.3 What the checker proves, and what only the runtime can know

This split is the honest core of the design.

**Proved statically, at admission:**

| # | Property | Mechanism |
|---|---|---|
| C1 | The delegator holds what it delegates. A mint that widens beyond the delegator's own cone is refused. | `cap_order.covers` over the held cone, the item-66/294 fold, unchanged |
| C2 | The delegatee's uses stay inside the delegated surface: a method outside `S` is a type error. | ordinary typing of `Delegate[S]` |
| C3 | The reference does not escape the receiving scope: not stored, not captured, not returned, not carried, not seated in spawn config, not in a handoff type. | 308 B1 clauses 1, 2, 3, 4, 6, 7 |
| C4 | The reference never appears in an inverse, compensate, or witnessed-argument position, so no accumulated obligation can depend on a live delegation. | 308 B1 clause 5, this note's D2 |
| C5 | The augmented delegation graph is acyclic. | G3, this note's D3 |
| C6 | Depth is bounded: a `Delegate[S]` may not cross a second boundary in v1. | this note's D4 (§6.2) |
| C7 | The ceiling is enumerable: the set of (delegator cone, delegatee) pairs is finite and header-computable. | G8, over service signatures plus the provider table |
| C8 | A crossing that requires a delegation and has none is refused at admission, not at runtime. | the 246 obligation shape, applied to `Delegate[S]` parameters |

**Knowable only at runtime, and therefore the gate's job:**

| # | Property | Mechanism |
|---|---|---|
| E1 | Whether a grant is live at THIS crossing: not expired, uses remaining, not revoked, generation current. | `_live_grant_for`, shipped |
| E2 | The actual narrowed valuation when it depends on runtime data (`path = job.dir`). The checker proves the static ceiling; the gate refuses a mint that widens past it. | `cap_order.covers` at the mint, runtime side |
| E3 | Consumption accounting, and its durability across a crash. | `_consume_grant`, consume-before-fire WAL ordering, shipped |
| E4 | Whether a revocation raced a crossing, and which side of the fence it fell on. | the crossing's own liveness check, §3.4 row 3 |
| E5 | Across a process seam: whether a presented receipt is one this receiver's own ledger admits. | 337's selector rule, §9 |
| E6 | The receipt an operator reads: who lent what to whom, when, how narrowed, spent how many times, retired when and by what. | the grant ledger row plus the WAL |

The line between the tables is the line between a ceiling and an actual. **The
checker proves the ceiling; the ledger records the actual.** G8 enumerates the
ceiling, which is why the audit surface stays enumerable even though the
narrowings are dynamic.

---

## 5. Q6: the surface, and why there is no new verb

### 5.1 The mint is `effect lease`, extended to name a key

The shipped lease form already is the mint. It needs two changes, both small:

1. its capability may be named by a **wiring key**, resolved to declared tokens
   through the key-to-token bridge `_cap_keyed` that item 421 F1 already built
   for exactly this translation;
2. the resulting handle is **typed** `Delegate[S]`, where `S` is the service
   type of that key, rather than an opaque binding on which only `.revoke()` is
   structurally admissible.

```revl sketch
component Ingest requires fs: FileSystem, worker: Worker {
  let d = effect lease fs(path = config.job_dir) ttl 5m uses 20
          undo d.revoke()
  let n = effect worker.run(config.job, d)
}
```

Nothing else about the form changes. The acquisition stays class-(c)
ticket-gated so a program cannot mint its own consent. The disposer stays
exactly `<bind>.revoke()`, the own-requestId revoke, which is the one exempt
disposer. The grant row, the liveness axes, the consume-before-fire ordering,
the generation binding and the 379 revoke verb are all reused unchanged.

**`Delegate[S]` is a type constructor, not a lexer keyword**, exactly as
`Approval[C]` and `Opt[T]` are, so the self-hosted lexer's keyword-set parity
oracle is untouched and no program using `delegate` as an identifier breaks.
That is the same argument 243 made for `witnessed` and 308 made for `shared`.

### 5.2 Why not a `delegate` verb

A dedicated verb would have to mint something, expire something, revoke
something, bound something by uses, bind something to a generation, and write a
receipt an operator can read. Every one of those exists in the grant ledger. A
second implementation of them is the parallel-mechanism shape the 294 note
refuses at every fork, and it would be a worse copy: the ledger's two hard parts
are durable consume-before-fire accounting and a crash backstop so a killed
holder cannot pin authority forever, and a fresh mechanism re-derives both.

There is a second reason, specific to this item. A verb suggests delegation is
an EVENT the language performs. It is not. It is a value with a type, produced
by an acquisition and consumed by a parameter, and every guarantee in §4.3 comes
from treating it that way. Making it a verb would put it back on the wiring
plane, where G2, G3 and G7 all have opinions, which is precisely the collision
Q1 is about.

### 5.3 What the operator reads

The receipt is the grant ledger row with two fields added:

```
{
  "requestId": ...,
  "capability": "fs.write(path=\"/var/spool/job-42\")",
  "subject": "Worker",                  # NEW: who it was lent to
  "chain": [{"from": "Ingest", "to": "Worker", "narrowed": ["path"]}],  # NEW
  "component": "Ingest",                # the delegator, unchanged meaning
  "candidateHash": ...,                 # generation binding, unchanged
  "session": ...,
  "grantedAt": ..., "expiresAt": ...,
  "remainingUses": 20, "consumed": 0, "revoked": false
}
```

`subject` is the entire delta between a 294 lease and a 442 delegation at the
data level. `chain` is shaped for depth from day one even though v1 writes
exactly one link (§6.2).

---

## 6. The item's remaining open questions

### 6.1 Q2: a delegation shares, it never transfers

**Decision: share.** The item's own framing is right that sharing needs no G2
change and transfer does, and transfer is the wrong model here for three
independent reasons:

1. **G2's unit is `(key, realm)`** and its provider table is built at link time
   (`provider_of`, `lower.py:9426`, one provider per pair, `lower.py:9443`). A
   transfer would make that table dynamic, which is a wiring-plane change, and
   426 §5.2 is the note that says why it is blocked: a dynamic provider change
   requires fiber disposal, LIFO-correct teardown replay, and consumer
   re-resolution.
2. **308 already decided the vocabulary.** A delegated reference is exactly the
   `borrowed` mode: positional, confined by B1, registering nothing in any
   accumulator. 308 reserved `transfer` as a source-level marker for the case
   where the final inverse changes hands, and deferred it. A delegation never
   changes who runs the final inverse: the delegator keeps every bracket it
   holds.
3. **The item's whole point is scoping.** Transfer is the opposite of scoped: it
   permanently relocates authority. Whatever `delegate` means, it does not mean
   that.

So the delegated thing is a bounded VIEW of an authority, held by the delegator
throughout, and the delegatee holds nothing when the call returns.

### 6.2 Q3: depth chains, and v1 bounds it at one

The item is right that an unbounded chain is a cone nobody can enumerate, which
breaks G8. The bound is available without a counter, because the graph is
finite: the set of service methods declaring a `Delegate[S]` parameter is
header-computable, so the transitive closure of possible hops is a finite DAG
(D3 already keeps it acyclic) and G8 can enumerate the ceiling at any depth.

Nevertheless:

**Decision D4: in v1, a `Delegate[S]` value may not be passed across a second
crossing.** Passing it to a plain `fn` inside the same component is admitted
(that is not a new party, and 308 already admits passing a borrow down);
passing it as an argument to a service-method call is refused. The line is the
existing crossing / non-crossing distinction, so there is no new concept and no
depth counter to get wrong.

**The receipt chains rather than flattens**, from day one, even at length one.
Flattening would destroy the audit chain, and the audit chain is what an
operator actually needs: "who lent this to whom, and through whom" is the
question a receipt exists to answer. Item 251's distillation and the item-249
provenance work both assume a chain shape.

The honest caveat for depth > 1, stated now so nobody rediscovers it: the edge
graph is over components, not instances. With `spawn`, one component edge is
many instance edges. The CEILING stays enumerable, which is what G8 claims; the
instance-level chain is a runtime artifact and lives in the ledger, not in the
audit surface.

---

## 7. Q5: is this 294's lease under another name?

**Substantially yes, and stating so is the useful outcome.**

| part of 442 | where it already lives | new work |
|---|---|---|
| Bounded authority (uses, ttl) | 294 lease / 344 grant | none |
| Revocation, early and monotone | 379 `revoke_standing_grant`, 294 own-requestId disposer | none |
| Expiry checked at the crossing | `_live_grant_for` invariant 3 | none |
| Consume-before-fire durability | `_consume_grant`, WAL ordering | none |
| Auto-lapse when the holder is swapped | `candidateHash` generation liveness | none |
| No self-minting of consent | class-(c) ticket-gated acquisition | none |
| Narrowing algebra, and no widening | `cap_order.covers`, item 66/294, F1's token bridge | none |
| The receipt an operator reads | the grant ledger row + WAL | two fields (§5.3) |
| Attenuating to a CHILD | `_check_spawn_attenuation` | none |
| Confining the reference to a scope | 308 B1, seven clauses | apply to a new tainted type |
| An unforgeable typed authority value | 246 `Approval[C]` | generalize from token to provision |
| **Naming a second party** | **nowhere** | **`Delegate[S]` + the parameter position** |
| **Keeping the second party acyclic** | **nowhere** | **D3, the augmented G3 check** |

Two rows are genuinely new, and they are the item. Everything above them is
assembly.

The precise statement, because "it collapses into 294" would be too strong:
**294's lease is reflexive and 442's is transverse.** A lease narrows what the
acquiring frame may already do; its holder and its subject are the same scope.
A delegation names a subject that is not the holder. That is a real difference,
it is the one thing the shipped capability model cannot express at all, and it
costs one type constructor, one parameter position, and one graph rule.

So 442 should not be closed as a duplicate of 294. It should be **rescoped from
"a new language-level crossing with a gate-minted receipt" to "a type on 294's
lease handle, and a parameter position that accepts it"**, and its size should
drop from LARGE to something a single slice can carry.

---

## 8. The honest ledger

### 8.1 442 does not add reach; it subtracts it

The G8 ceiling under this design is the same reach set you get by wiring the
delegatee to the provision directly, because D3 requires the augmented graph to
be one G3 already permits, and option 1 in §2 (widen `requires`) reaches exactly
that graph. So the audit surface does not shrink.

What shrinks is the ACTUAL, along three axes the static surface cannot express:

- **time**: the reference exists for one call, and the grant for one ttl,
  instead of for the delegatee's whole activation lifetime;
- **count**: `uses n`, spent durably;
- **value**: `fs(path = config.job_dir)` narrows to the directory THIS job
  owns, which a `requires` edge cannot say because the value is not known until
  the job exists.

That third axis is the strongest single argument for the feature and it is
worth being precise about: parameterized capabilities (294) made a narrowing
expressible, but only at a declaration site, where the value must be static. A
delegation is the first construct that can narrow by a value the program
computes. **G8 enumerates the ceiling; the receipt records the actual.**

Anyone evaluating this item should hold it to that claim and not to a reach
reduction it does not deliver.

### 8.2 Checked, enforced, declared

Following 294's own ledger discipline.

| property | status |
|---|---|
| No widening at the mint | **checked** statically for literal and `config.`-valued narrowings; **enforced** at the mint by the gate for runtime-valued ones |
| The reference does not escape | **checked** (308 B1) |
| No inverse depends on a live delegation | **checked** (D2) |
| Acyclicity | **checked** (D3, over G3) |
| Depth <= 1 | **checked** (D4) |
| Expiry, uses, revocation | **enforced** by the ledger at each crossing |
| Generation lapse on swap | **enforced**, already shipped |
| That the delegatee's HOST bodies honour the cone | **declared**. This is item 411's mount enforcement, scoped there, not claimed here |
| That an extern behind the delegated service does not retain the reference | **declared**. 308's F10 retaining-extern audit is an open followup and applies unchanged |

The last two rows are the same honesty line 308 draws for O1: the declaration
surface is the proof surface, and a host author who retains out of band has lied
to it.

### 8.3 Named limitations

1. **The narrowing is enforced only where 411 is.** For a `uses`/`ttl`/revoked
   bound, the ledger enforces it and the enforcement is real today. For a
   `path=` bound, enforcement is 294 Slice 3 / 411 Slice 2, which is deferred.
   Until then a path narrowing on a delegation is exactly as strong as a path
   narrowing on a lease: declared, audited, diffed, and not mounted.
2. **Revocation is a crossing fence, not a time fence** (§3.4 row 3). The same
   statement 294 makes about realm revocation.
3. **The delegatee cannot be given a reference it may hold across calls.** That
   is deliberate (C3) and it is the same restriction 308 accepted: a program
   that needs a longer-lived reference restructures so the holder lends per
   call. If that restriction turns out to be wrong, the fix is `shared`, which
   308 already deferred to 294's leases, and not a weakening here.
4. **Delegating a key you provide, to a component you call, is refused** (D3).
   Some readings of the item's "one of its own provisions" want that shape; it
   is a G3 cycle and it is refused today by the item's own named alternative.
5. **Remote delegation is not designed here**, only bounded. §9.

---

## 9. The remote half, and why it waits for 439

The item says 439 and 442 are the same feature from two ends, and that is right
about the motivation: a remote provider where every call needs its own approval
is the ergonomic failure that pushes people to over-grant. The 344 standing
grant already answers that shape in the small: one approval, N bounded
handovers, each recorded.

Crossing a process changes exactly one thing, and 337's amendment already
decided it. **A receipt on the wire is a selector, never a bearer token.** The
receiver looks the grant up in its own ledger and re-decides from state it holds
independently; a receipt the receiver's own state does not admit is refused,
fail-closed, with no blip. A forged or replayed receipt refuses for the same
reason an unbound selector does today: the receiver never trusts the wire's copy
of the answer.

The consequence is a real dependency, not a detail: **two processes can only
share a delegation if they share a ledger, or if their ledgers are federated.**
That is unbuilt, and it is the honest reason the remote half waits. For an
external agent over A2A the problem is harder still, because 439's own open
question 2 applies: an external agent is not a revl composition, so nothing
about it is checked, and a delegation to it is a delegation to an untrusted
author (item 329) whose only enforcement is the ledger's own bounds.

Recommendation: **design the remote half inside 439, not here.** The in-process
design above is complete, buildable, and independently valuable, and it does not
prejudge the federation question.

---

## 10. Slices

**S1. The typed handle, in-process. Buildable today, depends on nothing.**
`Delegate[S]` as a type constructor; `effect lease` extended to name a wiring
key through `_cap_keyed`; the handle typed rather than opaque; `Delegate[S]`
admitted as a service-method parameter type; `Delegate[S]` added to the
resource taint base so 308's B1 walk visits it; D2's clause-5 diagnostic; D3's
augmented G3 check; D4's depth bound. The ledger gains `subject` and `chain`.
This is the whole in-process feature and it is one slice.

**S2. The audit surface.** The delegation ceiling in `revl audit`, the receipt
in the operator panel, and `audit --diff` flagging a widened ceiling the way it
flags a weakened declared reach today. Presentation only, no admission change.
Reads better on top of 426 S1's row table but does not require it.

**S3. Enforcement of value narrowings.** Nothing of its own: this is 294 Slice
3 / 411 Slice 2, and a delegated `path=` bound becomes enforced exactly when a
leased one does. Listed so nobody plans it twice.

**S4. Remote.** Owned by 439. §9.

**Explicitly not in 442:** transfer of a provision (§6.1, and 308 reserved the
marker); depth > 1 (§6.2); a delegation held across calls (§8.3 item 3, and
that is `shared`, deferred to 294); ledger federation (§9).

---

## 11. Exit tests

The item to prove is the one in the brief: that a delegation is **genuinely
scoped and genuinely revocable, rather than merely declared**. Tests 1 and 2 are
the ones that carry that weight; the rest close the design's other claims.

1. **The crux test: revocation and G7 hold simultaneously.** `A` mints a
   delegation with `uses 2` and hands it to `B` in one call. `B` performs two
   crossings through it, each registering an inverse on `B`'s activation stack.
   `A` revokes. `B` attempts a third crossing, and the frame then aborts. Assert
   all three, in one run:
   (a) the third crossing is **refused at the seam**, naming the retired grant,
   not silently succeeding and not silently no-opping;
   (b) the abort replays **both** accumulated inverses, LIFO, exactly once, with
   no `bracket-fault`, even though the delegation is dead;
   (c) the WAL shows both consumes ordered before their fires and the revoke
   record after both, so no crash cut exists at which an inverse is owed under a
   live delegation, or a fire is recorded without its consume.
   Test (b) is what makes revocation real rather than a promise, and (a) is what
   makes it more than a declaration.

2. **Revocation is enforced by the runtime, not only by the checker.** A
   hand-built IR that performs a crossing through a retired delegation, with no
   checker run at all, is **refused at the crossing** by the frame check. This
   is 246 test 15's shape and it is the test that separates "genuinely
   revocable" from "declared revocable": if only the static rule stops it, the
   feature is advisory.

3. **Scope is enforced, one case per B1 clause.** The delegated reference stored
   in activation state, captured by a closure, returned from a provide method,
   placed in a record that then escapes, seated in `spawn` config, carried by a
   `handoff` type, and (clause 5) placed in an `undo`, a witnessed argument, and
   a `compensate` expression: nine refusals, each naming the clause and the
   delegated type. After the call returns there exists no admitted program in
   which `B` can name the reference.

4. **No widening at the mint.** A mint of `fs(path="/")` under a holder of
   `fs(path="/tmp")` is refused by `covers`, naming both cones. A runtime-valued
   narrowing that widens is refused by the gate at the mint. A mint of a key the
   delegator does not hold at all is refused.

5. **Acyclicity.** A component delegating a key it itself provides to a
   component it calls is refused, naming both edges of the cycle (D3). The same
   topology expressed by widening the delegatee's `requires` is refused by G3
   today, and the two diagnostics agree about the cycle.

6. **Depth is bounded.** Passing a `Delegate[S]` to a further service-method
   call is refused; passing it to a plain `fn` in the same component is
   admitted. The receipt's `chain` has exactly one link.

7. **Generation lapse is free.** Swapping the delegator retires every grant it
   minted, and the delegatee's next crossing refuses, with no
   delegation-specific bookkeeping in the swap path.

8. **The ceiling is enumerable and stable.** `revl audit` prints the delegation
   ceiling from headers alone, without lowering a body; two runs with different
   runtime narrowings produce the same ceiling and different receipts; and a
   service signature that adds a `Delegate[S]` parameter shows in `audit --diff`
   as a widened ceiling.

9. **G1 and G2 are untouched.** A delegatee holding a `Delegate[S]` parameter
   still cannot name the underlying key anywhere else in its body (G1 refusal),
   and the provider table is byte-identical to the same composition compiled
   without any delegation.

10. **No new runtime.** The delegation path allocates no ledger, no counter and
    no revocation predicate of its own: assert that a delegation's grant is
    retired by the identical `_grant_covers` predicate a 294 lease's is, so mint
    and revoke can never disagree (the 245/246 F1 over-coverage hole stays
    closed on that one line).

---

## 12. Recommendation

1. **Rescope item 442** from "a new language-level crossing" to "a type on 294's
   lease handle plus a parameter position that accepts it". LARGE becomes one
   slice (§10 S1).
2. **Unblock it from 426 R1** (§3.5). Revocation retires a ledger row; it is not
   a withdrawal, and the fiber lifecycle question 426 R1 owns is not reached.
3. **Do not build it yet, but not because it is blocked.** Its ergonomic payoff
   is largest after 439, its value-narrowing payoff is only enforced after 294
   Slice 3 / 411 Slice 2, and the in-process feature is small enough to land
   quickly once either is in view. Build S1 when the first of those two arrives.
4. **Record the finding in 246.** Open question 3 is answered: it should be a
   first-class delegation, and the binding is the parameter type 246 declined to
   add.
