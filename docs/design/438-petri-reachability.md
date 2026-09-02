# 438: Petri-net reachability as a compile-time liveness analyzer

Design note for roadmap item 438. Design only: no compiler change, no `src/`
change. One test file lands with it, `tests/test_438_liveness_shapes.py`: a
falsification harness rather than a prototype, in which every structural claim
below is asserted mechanically, so the recommendation can be re-checked by
running the tests instead of by rereading the argument. It also carries the
reproducer for the one liveness bug this pass found.

**The recommendation is DO NOT BUILD the engine, and DO build one linear check
the search would have found.** Not because reachability is hard, and not
because the item is wrong about the gap it names. Because the net a revl
composition produces today is conflict-free, monotone and 1-safe over an
acyclic graph, and on that class of net the reachability question is already
answered by the linear-time topological sort `_link` runs to compute
`loadOrder`. Both shapes item 438 names were constructed and run against
`origin/main`: one is already a refusal with a named cycle, and the other has
no spelling in the language yet.

**The search for a wait edge G3 does not see found one** (§5.2). G3 is acyclic
over COMPONENTS; placement quotients that graph by a process partition, and a
quotient of a DAG can have a cycle. A process wires every proxy before it
serves anything, so two processes that require each other both block on a
listener that never opens. Nothing checks the process graph. That is the item's
own question - a composition that is structurally admissible reaching a dead
state - answered in the affirmative, and the check for it is a cycle detection
on a graph with one node per process, not a marking-graph search. §8.1 is
that check. §7 says what would have to land before the engine itself is worth
building.

---

## 0. The three open questions, answered

| # | Question | Answer | Section |
|---|---|---|---|
| 1 | What is a place: a key, a `(key, realm)` pair, or a coeffect? | `(key, realm)`. It is G2's unit and `provider_of`'s key, and the row table already carries claims in that shape. **The choice does not matter**, because of answer 2. | §2 |
| 2 | Bounded BFS needs a bound, and a bounded refusal is a partial guarantee that must say so. | Moot. The net is **conflict-free, monotone and 1-safe over a DAG**, so reachability is `O(V+E)` and there is no bound to declare. The bounded search would enumerate one marking per order ideal of the dependency DAG to re-derive a fact the Kahn sort in `_link` already has. | §3, §4 |
| 3 | Does a dead state refuse, or warn? | **Warn by default, refuse per shape.** An approximate search may only warn. A shape whose check is exact and finite may be promoted to a refusal on its own, which is what §8.1's process-graph cycle is: a plan-time refusal naming both processes and the two keys, the same shape as G3's message. | §6, §8, §9 |
| - | So what gets built? | The process-graph acyclicity check (§8.1), plus the wait-edge inventory test that keeps §5's table honest. Small. Not the net engine. | §8, §11 |

And the question the item did not ask, which is the one that decides it:

| # | Question | Answer |
|---|---|---|
| 4 | Is the derived net's arc from a provision to its consumer a **consuming** arc or a **read** arc? | A read arc. A provision is resolved once and held; nothing spends it, and G2 constrains providers, not consumers. Every interesting property of Petri reachability - conflict, non-monotonicity, the PSPACE-hardness, the need for S-invariants - comes from consuming arcs. revl's composition IR has none. | §2.3 |

---

## 1. What the composition IR gives you today

### 1.1 On `main`

`_link` (`src/revl/lower.py:9932`) builds, from the lowered components:

- `entries`, one manifest dict per component (`lower.py:9970`), carrying
  `name`, `file`, `inject` (the required wiring keys, sorted), `provides`, and
  optionally `isolate`, `intercept`, `routes`.
- **`provider_of: dict[(key, realm) -> component]`** (`lower.py:10022`), with
  `_realm(entry, key)` reading `entry["isolate"]` and defaulting to
  `SHARED_REALM = ""` (`lower.py:10015`). G2's refusal is a collision in this
  dict (`lower.py:10022-10049`).
- `graph`, `indegree` and `edge_key` (`lower.py:10086-10091`), the
  provider -> consumer edge relation with the key that carries each edge.
- The G3 coloured DFS (`lower.py:10138-10176`), which refuses a cycle by name
  with a `WhyTrace(kind="dependency-cycle", shape=CHAIN)`, one report per SCC.
- `loadOrder`, a Kahn topological sort (`lower.py:10182-10190`).

That is already, exactly, a marked graph with its firing order computed.

### 1.2 On `feat/426-composition-layers` (PR #150, item 426 S1)

The row table makes a composition a first-class checked declaration. `Row`
(`composition.py:129`) carries `label`, `origin`, `source`, `component`,
`claims: list[(key, realm)]`, `extra_claims`, `requires`, `config`, `granted`.
`RowTable.wiring()` (`composition.py:196`) is the rename-invariant projection
`{qualified_label: {claims, requires}}`, and `compile_composition`
(`composition.py:442`) lands the table at `document["rows"]` and
`document["manifest"]["rows"]`.

This is the structure the item wants, and it does make the net derivable one
level up from the linker. It changes nothing about the answer, because the row
table's claims are the same `(key, realm)` pairs `provider_of` is keyed on -
deliberately, per 426 §1.1.

**One seam to record if `analyze` is ever written.** `requires` is spelled
three ways along the pipeline: `ComponentDecl.requires` is
`list[(local, service, line)]` (`parser.py:466`), the lowered
`comp["requires"]` is `dict[qualified_key -> service]` (`lower.py:8751`), and
the manifest's `entry["inject"]` is a sorted list of qualified keys
(`lower.py:9991`). `Row.requires` on the branch is the sorted list of **local
binding names** (`composition.py:376`), not qualified wiring keys. Joining row
wiring against the linked manifest needs that conversion, and getting it wrong
fabricates unsatisfied requirements.

---

## 2. The net, derived

### 2.1 The mapping

| net object | revl object |
|---|---|
| place | one provision, `(key, realm)` |
| transition | one component activation |
| produce arc `t -> p` | `t`'s component provides `key` in `realm` |
| read arc `p ~ t` | `t`'s component injects `key`, resolved in `t`'s realm |
| initial marking | empty: nothing is activated |

`tests/test_438_liveness_shapes.py::_net` implements this in twenty lines
directly off `ir["manifest"]`, mirroring `_realm`.

### 2.2 Why the coeffect is not a separate place kind

Item 438's open question 1 offers "or a coeffect". A coeffect that is a
required provision is already the read arc above. A coeffect that is not - a
capability token, a clock, a config field - has no producing transition inside
the composition and no consumer that can be blocked by its absence at
activation time, so it contributes an always-marked place and no constraint.
Modelling it adds places and removes nothing.

### 2.3 The arc kind, which is the whole answer

A provision is **resolved once and held for the consumer's lifetime**. Nothing
spends it. Two consumers of one key are ordinary: G2 forbids two *providers* of
one `(key, realm)`, and says nothing about consumers. So the input arc is a
test arc, modelled as a self-loop: enablement checks the token, firing puts it
back.

This is not a modelling convenience chosen to make the answer come out. It is
what `docs/parallel-activation.md` already asserts and what
`src/revl/activation.py` already relies on: two independent DAG branches are
provably independent, because activating them concurrently cannot race, there
being no shared mutable resolution between them. That is exactly the statement
that the net has no conflict, and §46 shipped a scheduler on the strength of
it.

---

## 3. Four properties, and what they collapse

Read off the derivation, and asserted in the test file.

**P1. One producer per place (G2).** `provider_of` admits one component per
`(key, realm)`, so every place has at most one producing transition, and each
transition fires at most once. The net is **1-safe by construction**.
(`test_g2_gives_every_place_exactly_one_producing_transition`)

**P2. No transition removes a token (read arcs).** The marking only grows. The
net is **monotone**. (`test_the_net_is_monotone_and_conflict_free`)

**P3. No conflict.** Two transitions never compete for a token, because
nothing takes one. An enabled transition stays enabled until it fires, which is
**persistence**. (same test)

**P4. No cycle (G3).** The transition dependency relation is acyclic, and
`loadOrder` is a witness firing sequence.
(`test_load_order_is_a_witness_firing_sequence`)

### 3.1 The consequences

**C1. There is a unique maximal marking and every maximal firing sequence
reaches it.** Persistence plus monotonicity is the diamond property: if two
transitions are enabled, firing one leaves the other enabled, so any two
maximal sequences converge. This is the classical Keller/Church-Rosser result
for persistent nets, and the test asserts it by brute force over every
permutation of a four-component diamond.

**C2. Deadlock-freedom is equivalent to acyclicity, and is decided in
`O(V+E)`.** For this net, over components. §5.2 is the same statement applied
to a DIFFERENT graph, the process quotient, where acyclicity is not checked and
therefore deadlock-freedom does not hold. A marking is dead when no transition is enabled and some transition
has not fired, which means some unfired transition's read set is unmarked
forever, which means some place it reads has no producing transition or has one
that is itself blocked. Chase that back: either you reach a place with no
producer (§6), or you close a cycle, which G3 refused before the net existed.

**C3. The S-invariant computation is vacuous.** `C = Post - Pre`. A read arc is
a self-loop, so it cancels; every remaining entry is a `+1` at a place and its
single producer. A non-negative `y` with `y^T C = 0` therefore satisfies, for
each transition `t`, `sum(y_p for p in post(t)) == 0`, which forces `y_p == 0`
at every produced place. **The invariant space is spanned by the unproduced
places alone** - and an unproduced place is a required key nothing provides,
which `query.Composition.unresolved_injections` (`query.py:240`) answers with a
dict lookup. Martinez-Silva signed elimination over a revl composition returns
that set and nothing else.
(`test_the_incidence_matrix_makes_the_s_invariant_space_trivial`)

Boundedness, the property the item wants S-invariants for, is P1. It is free.

### 3.2 What the bounded BFS would actually cost

The marking graph has one state per order ideal of the dependency DAG, which is
exponential in the DAG's *width* - the number of mutually independent
components, which in a real composition is most of them. The search would
enumerate that lattice to report the single terminal state that `loadOrder`
names in one linear pass, with no cycle to name, because a composition with a
cycle never reaches the analyzer.
(`test_the_marking_graph_search_finds_nothing_the_sort_did_not`)

So the honest tractability answer is not "PSPACE-hard, therefore approximate".
It is: **not hard, and not because the analysis is clever, but because the
structural gates already collapsed the state space to one
trajectory-equivalence class.** An analyzer built on this IR would be a large,
correct engine that provably cannot report anything.

---

## 4. The two shapes, constructed and run

Both are in `tests/test_438_liveness_shapes.py`. Both were run against
`origin/main` at `00bf8336`.

### 4.1 Mutual wait: already a refusal, with the message the item asks for

```revl reject G3
service Ledger { fn balance() -> Int }
service Audit  { fn record() -> Int }

component LedgerSvc requires audit: Audit provides ledger: Ledger {
  provide ledger { fn balance() = 1 }
}

component AuditSvc requires ledger: Ledger provides audit: Audit {
  provide audit { fn record() = 2 }
}
```

```
error: mutual.rvl:4: dependency cycle: LedgerSvc -> AuditSvc -> LedgerSvc (G3)
  why `LedgerSvc` is in a dependency cycle:
    LedgerSvc -> AuditSvc -> LedgerSvc
      LedgerSvc  mutual.rvl:4   provides `ledger`
      AuditSvc   mutual.rvl:10  provides `audit`
      LedgerSvc  mutual.rvl:4
```

Item 438 asks for "a REFUSAL naming the deadlocked cycle rather than a crash at
3am". That is the refusal, it names every hop and the key that carries it, it
is linear time, and it ships. The item's framing - "none of them answers a
REACHABILITY question" - is true of G3 as *stated*, and false of G3 as
*implemented on this net class*: on a conflict-free monotone net, acyclicity IS
the reachability answer (C2).

The gap would be a wait edge that is not a `requires` edge. §5 is the search
for one.

### 4.2 Starved merge fan-in: no spelling exists

Within one activation, the shape is a refusal:

```revl reject lifecycle
service Feed { fn tick() -> Int }

component Fanin provides feed: Feed {
  let a = effect Stream.source() undo a.close()
  let b = effect Stream.source() undo b.close()
  let first = subscribe a undo first.close()
  let both  = subscribe merge(a, b) undo both.close()

  provide feed { fn tick() = 1 }
}
```

```
error: starve.rvl:7: stream source `a` is already subscribed - a subscription
  is single-consumer (rule 3.1)
```

`_admit_stream_operand` (`lower.py:7479`) checks rule 3.1 against
`env.subscribed_sources` and rule 3.6 against `env.terminal_stream_sources`.
The item calls this "pointwise", and it is - but pointwise is *complete* here,
because the scope it ranges over is the scope a stream can exist in.

The item's premise is that the sources are "consumed elsewhere". For that,
a stream has to be reachable from two components. It cannot be. A stream is an
`env.host_locals` entry created by `effect Stream.source()` (`lower.py:8505`,
`_HOST_CALLABLES` at `lower.py:676`); there is no `Stream[T]` provision and no
`Stream[T]` coeffect. Every spelling that tries to hand one across a component
boundary is refused, and the refusal says why:

| spelling | refusal |
|---|---|
| `let s = emit src.open()` | G4: a plain value bind has no place in an activation body |
| `let s = emit src.open() compensate s.close()` | same |
| `let s = effect src.open() undo s.close()` | rule 3.1's first check: "`subscribe` needs a stream source - the operand does not name one ... **A required `Stream[T]` capability is a later slice.**" |

The compiler's own hint names the missing slice. Until it lands, shape two is
not a false negative in the checker. It is a shape the language cannot express.
(`test_a_stream_cannot_cross_a_component_boundary`)

---

## 5. The residual: where a wait could live that G3 does not see

A deadlock needs a cycle of wait edges. G3 covers exactly one wait relation:
"component B cannot activate until component A has provided key k". The
inventory below is the search for a second one, and **it found one** (§5.2).
This is the part of the note most likely to go stale, so it is a table with the
evidence rather than prose, and §8.2 makes it a test.

The checked surface has exactly four forms that suspend a fiber: `await
<expr>`, `effect await` / `emit await` (item 131), `<sub>.next()` (item 130),
and `await Job.run(...)`. Everything else in the table either does not wait or
waits on something G3 already orders.

| wait primitive | on `main`? | can the waited-for thing belong to another component? | covered by G3? |
|---|---|---|---|
| activation waits for a provider | yes, `_link` / `activation.local_prereqs` | yes, by definition | **yes**, this IS G3 |
| `emit svc.method()` on an injected key | yes | yes | **yes**, the call follows a `requires` edge, so a call cycle is a `requires` cycle |
| `emit` through a spawned handle | yes | **no**: the instance's provisions go into a fresh local realm nobody else can name (`lower.py:9630`), and the handle is owned by the spawning activation | **no**, and it does not need to be - see §5.1 |
| `emit` across a routed multi-realm key | yes, `RouteStmt` / `_routes` | yes | **yes**: `_link` adds one edge per routed realm (`lower.py:10100-10113`), so every leg is in the DAG |
| **cross-process seam call** | yes, `bridge._Client.call` | yes | **at the component level, yes. At the PROCESS level, no** - §5.2 |
| an arrow passed across a service boundary | yes | yes, and the re-entrant call edge `C -> P -> C's closure -> P` is invisible to G3 | **no**, and it cannot wait: an arrow reaching an async op is refused (A1), and `Async` is not admitted in a service signature, so the worst case is divergence, not deadlock |
| `next` on a subscription | yes | **no**: a stream cannot cross a component boundary (§4.2) | n/a |
| `block`-policy backpressure (provider suspends until the consumer drains) | yes, item 130 §4.4 | **no**: provider and consumer are host locals in one activation | n/a |
| `await` at a divert boundary (A1) | yes | **no**: `Async[T]` is position-restricted and is not a value type. There is no future, promise or task anywhere in `src/revl/`, so nothing one component awaits can be completed by another | n/a |
| `await approval[C] { ... }` | yes, `lower.py:9344` | despite the keyword this does not suspend: it is a ledger lookup that fails closed | n/a |
| parallel emission fan-out and rejoin (item 259) | yes, `parallel.py` | branches are proven independent before the partition (259 §2.2) and share no sync object | n/a - the partition is refused if not |
| `Pool` / `Job` | yes | **no**. `_HOST_ARG_SIG` (`typecheck.py:1197`) is the complete host frontier and it has no `acquire`/`release`: a pool connection is borrowed for one call, and exhaustion RAISES rather than blocking | n/a |
| any mutex, semaphore, channel or queue | **no such surface exists** | - | n/a |
| teardown | yes, `activation.teardown_lifo` | yes | **yes**: teardown is sequential over the reverse of a valid topological order, and `async` in an `undo` is statically refused (`lower.py:6881-6893`) |
| management leases (item 61) | yes, `mcp/leases.py` | yes | n/a - a lease is TTL-bound and governs the management plane only; the running component keeps serving every call throughout |

### 5.1 The one uncovered edge: spawned instances

Spawn is the single place a provision exists outside the static table. A
spawned instance's provisions "never enter the link-time G2/G3 table
(decision 5/6)" (`lower.py:9634`), so if two spawned instances could each wait
on the other's provision, G3 would not see it.

They cannot today, for two reasons worth writing down rather than assuming.

**The realm is fresh.** `_lower_spawn` isolates every key the target provides
"into a *fresh* local realm at spawn time so any number of instances coexist
without a G2 collision" (`lower.py:9630-9634`). A place nobody else can name is
a place nobody else can read, so the spawned instance's provisions are not
reachable by any other component's `inject`.

**The handle is owned.** A spawn handle rides in the `bind` of a `let-effect`
step and is owned by the spawning activation (G7/B1, `lower.py:8613`, `:9073`,
`:9679`); a call through it is an `emit` from the owner. So the wait relation
over spawned instances is a tree rooted at the owner, and a tree has no cycle.

What would break both at once is a spawned instance whose handle escapes to a
second component, which is trigger T4 in §7. This is a claim about a negative,
and the honest label for it is: **believed closed by fresh-realm isolation plus
ownership, not proved.** It is a row in the §8 test, and the ownership half is
the one to watch, because item 308's effect-ownership modes are precisely about
loosening it.

### 5.2 The finding: a process partition can close a cycle the component DAG does not

**This is the one wait cycle that is reachable on `main`, and nothing checks
for it.**

G3 is acyclic over COMPONENTS. Placement takes that DAG and quotients it by a
partition into processes, and **a quotient of a DAG can have a cycle**. Four
components in two disjoint chains, split crosswise:

| process | components | provides | requires from elsewhere |
|---|---|---|---|
| A | `P1`, `C1` | `k1`, `out1` | `k2`, from B |
| B | `P2`, `C2` | `k2`, `out2` | `k1`, from A |

The component graph is `P1 -> C2` and `P2 -> C1`, two disjoint edges. G3 passes
and `loadOrder` is produced. The process graph is `A -> B` and `B -> A`.

The boot order is what makes that a wait. `_process_runner.run` is three steps,
in this order: **(1)** wire every proxy for keys provided by other processes,
**(2)** activate this process's own components, **(3)** serve the keys other
processes need. A process therefore connects to all of its providers *before it
starts listening*. So A blocks in step 1 on B's socket and B blocks in step 1
on A's, and neither ever reaches step 3.

`bridge._connect`'s own docstring states the assumption this breaks: under
placement the processes start concurrently, so it retries "while the provider
comes up", which "makes start order irrelevant". That is true of a DAG of
processes and false of a cycle. The failure is bounded rather than eternal -
100 attempts at 50 ms, then a `ConnectionError` - so both processes die after
about five seconds instead of hanging. It is still a composition that admits
and cannot run.

**Nothing checked it** (fixed by issue 171; §8.1 has the landed surface).
`placement.py` derived each process's `proxies` from
`requires[pname]` minus `provides[pname]` with `owner.get(key)` naming the
target process, and refused a key no process provides. It never built the
process-to-process edge relation, and there was no cycle detection over it
anywhere in `placement.py` or `distribute.py`. The spec
comment says cross-process edges "are already resolved as proxies before local
activation, so they are (correctly) absent" from `depends` - which is the
assumption that connect ordering does not matter.

This is exactly the question item 438 poses: a composition that is structurally
admissible reaching a dead state that no structural gate sees. The answer is
that one exists, and that finding it costs a cycle check on a graph with one
node per process, which is the same DFS `_link` already runs one level down.
(`test_a_process_partition_can_close_a_cycle_the_component_dag_does_not`,
`test_the_same_components_placed_together_are_acyclic`)

### 5.3 Three residuals, named and not closed

Recorded so §8's table has its OPEN rows and nobody re-derives them.

**R1. Opaque host bodies dominate everything else.** A `@py { ... }` body is one
verbatim token to the compiler. Any `threading.Event().wait()`, lock, unbounded
socket read or subprocess wait lives there, invisible to every fold. This is
the same trust boundary G4, G8, item 414 and item 256 already rest on, and it
is out of reach of any IR-level net. Naming it matters because it is the reason
a liveness analyzer can never be a *guarantee* in revl, only a finder.

**R2. A hung synchronous compensation holds the whole teardown LIFO.**
`docs/design/teardown-contract.md` states it: the Phase-2 compensation seam is
synchronous, the deadline is checked *between* compensations, and on a tier
with no in-call preemption one hung compensation holds the abort until the host
call returns. Python and TypeScript have no in-call preemption. So component
A's `undo` calling a wedged host body holds B's teardown open indefinitely.
Documented and accepted, and it is R1 wearing a different hat.

**R3. `activation.local_prereqs` drops routed keys.** It reconstructs edges
from `inject` and `isolate` only (`activation.py:60-97`) and never reads
`entry["routes"]`, which `_link` does set and does use for edges. For a routed
key `_realm_of` returns the shared realm, so the per-realm route targets are
missed and `activate_concurrent` may start a routing consumer alongside its
realm providers. This is an UNDER-approximation of the wait set, so it cannot
deadlock - it removes order rather than adding it - but it can boot a consumer
against an unresolved routed leg. Worth its own issue; it is not a 438 problem.

### 5.4 The runtime already answers hang, at the tier where hang lives

`docs/seam-deadlines.md` (item 54) is the existing answer to "a provider can be
alive but wedged - deadlocked, GC-stalled, stuck on its own slow dependency".
Every seam call carries a deadline, a breach raises its own fault kind
`bridge.SeamDeadline`, distinguishable from peer death and from a returned
error, and placement always stamps a finite default "because a placed
composition is exactly where an unbounded cross-process wait is unacceptable".

That matters for scoping this item. The waits that can actually wedge a running
revl system are *below* the composition IR - inside activation bodies, at
seams, in host code - and a net derived from provisions and coeffects cannot
see any of them. Catching those needs a different net over a different IR (the
emission graph inside bodies, with deadlines as timed transitions), which is a
genuinely different and much larger item than the one 438 describes.

---

## 6. The one liveness fact the net does surface

A required key that nothing in the composition provides is an **unproduced
place**, and every transition that reads it is dead in every reachable marking.

It is not currently a refusal, and it should not become one:

```revl sketch
component LedgerSvc requires m: Missing provides ledger: Ledger { ... }
```

compiles, and produces `manifest.components[0].inject == ["m"]` with
`loadOrder == ["LedgerSvc"]`. That is deliberate. `_link` takes
`ambient_components`, and an open composition completed at admission time - a
hot swap compiling a lone changed file against a running manifest - is the
normal case (`compiler.compile_files(paths, manifest=...)`). Refusing here
would break the mechanism `docs/swap.md` is built on.
(`test_an_unprovided_place_admits_and_leaves_a_permanently_dead_transition`)

And it is already reported, twice, by shipped surfaces:

- `query.Composition.unresolved_injections` (`query.py:240`), consumed by
  `revl query reaches` (`query.py:680`), which adds the key to its
  `assumptions` and downgrades its own result to INCOMPLETE. That wording is
  item 418's posture done correctly, and it is the model for anything `analyze`
  would print.
- `revl dash` (`dash.py:171`) and `revl diff` / `revl changelog`
  (`composition_diff.py:163`, `:274`; `changelog.py:272`) as a broken
  dependency edge.

So even the one non-trivial fact in the net formulation is already on screen.

---

## 7. Triggers: what has to be true before this is worth building

The recommendation is "not yet", not "never". Each trigger below turns one of
§3's four properties false, and the first one to land should reopen this note.

**T1. `Stream[T]` becomes a provision or a coeffect** (item 130's "later
slice", named in `lower.py:7503`). Then rule 3.1's single-consumer claim stops
being a fact about one `Env` and becomes a fact about a composition, and
`_admit_stream_operand` cannot check it pointwise any more. This is the trigger
for item 438's own second shape, and it is the most likely to arrive. It breaks
P2 and P3: a subscription CONSUMES the right to subscribe, so the arc becomes
consuming and two consumers are in conflict.

**T2. A provision becomes exclusive or affine.** Any provision whose
acquisition denies it to a second consumer is a consuming arc. Breaks P2 and
P3, and is the point at which the marking graph stops being a lattice.

**T3. A bounded resource is shared across components** - a connection pool, a
worker slot, anything with a count. Not today: `Pool` and `Job` are in
`_HOST_CALLABLES` alongside `Stream` (`lower.py:676`), so a pool is acquired by
one activation and lives in its `env.host_locals` exactly the way a stream
does. Breaks P1 when it changes (a place holds `k` tokens, not one), so
1-safety is gone and S-invariants become the right tool for boundedness rather
than a vacuous computation.

**T4. A wait edge appears between two components that is not a `requires`
edge.** The candidate is §5.1: a spawn handle escaping to a second component,
or any handle-passing that lets B block on something A owns without B declaring
A's key. Breaks P4, and it is the only trigger that makes a cycle possible
again *in this net*. A cycle in the process quotient needs no trigger at all -
it is reachable today, which is §5.2, and it is why §8.1 is not gated on any of
these.

**T5. Interception or a layer can redirect a provision at admission time**
(426 S2's `replace`). If the fold can point a consumer at a different provider
than the one its claim named, the DAG the analyzer checks and the DAG the
runtime wires can differ. 426 §3.3 keeps the fold out of the gate and re-runs
`_link` unchanged, which is what closes this today; the trigger is any change
to that.

**One trigger is enough.** T1 alone justifies the engine, because it makes the
item's own worked example real.

---

## 8. What to build instead, now

Two things, both small, and neither is a net.

### 8.1 G3 over the process graph, at plan time (the one that finds a bug)

**Status: landed (issue 171).** `placement.process_cycle_refusal`, called from
`run_placement` once `owner` and `remote_specs` are known and before any TLS
material is minted or any child is spawned. Tests: §5's two compositions and
the message shape in `tests/test_438_liveness_shapes.py`. The rest of this
section is the specification it was built to.

`placement.py` already computes, per process, the set of keys it proxies and
which process owns each (`placement.py:2216-2221`). One node per process, one
edge per proxy, and the same coloured DFS `_link` runs. A cycle is a
**refusal**, not a warning, because the check is exact: the graph is finite,
derived, and has no approximation in it. Promoting this one shape while the
general analysis stays warn-only is the per-shape rule in §9, not an exception
to it.

The message follows G3's, one level up:

```
error: process cycle: A -> B -> A
  A proxies `k2` from B  (required by C1, provided by P2)
  B proxies `k1` from A  (required by C2, provided by P1)
  a process wires every proxy before it serves, so neither ever listens.
  fix: co-locate one of the two chains, or split the partition along the
  component DAG instead of across it.
```

Where it goes: the placement gate, next to the existing "key required by
`pname` is provided by no process" refusal, so it fires on `revl plan` before
any process is spawned rather than five seconds into a boot. It costs one
function and one refusal string, and it needs no `revl analyze` subcommand.

Two decisions to make when building it, recorded so they are not made silently:

- **`remote` seams are excluded.** A `[remotes.<key>]` provider is a separate
  composition on its own placement (item 151), reached by address; this process
  graph cannot see it and must not pretend to. The refusal covers
  same-composition proxies only, and says so.
- **The check is on the partition, not on the code.** The same components in a
  different partition are fine (`test_the_same_components_placed_together_
  is_acyclic`), so the refusal names the placement file and the processes, not
  the components' authors.

### 8.2 The wait-edge inventory test

In the shape item 414 established: not a proof, a completeness checklist the
type system cannot forget.

`tests/test_438_liveness_shapes.py` as landed is the first half - the two
shapes, the four net properties, and the process-cycle finding. The second half
is one table:

```
WAIT_EDGES = [
    (id, builder, verdict)
    # verdict is one of:
    #   "g3"        the edge is a requires edge; a cycle is refused by _link
    #   "scoped"    the waited-for thing cannot leave one activation
    #   "owned"     the wait relation is a tree rooted at an owner (spawn)
    #   "deadline"  bounded at runtime by a seam deadline (item 54)
    #   "diverges"  a G3-invisible call edge that provably cannot suspend
    #   "process"   covered by 8.1 once it lands, OPEN until then
    #   "host"      inside an opaque host body: R1, out of reach in principle
    #   "OPEN"      none of the above - a real hole
]
```

with a guard test that fails when a new wait primitive is added without a
verdict, so the enumeration cannot silently rot. That converts §5's table from
"re-derived by hand each pass" into "asserted every CI run", and it is the
mechanism that will *tell you* when a trigger in §7 lands, rather than leaving
it to be noticed. §5.2 is the argument for it: the process cycle was found by
walking the table, and nothing else would have found it.

Cost of both together: small. Compare with the engine: net derivation,
marking-graph search with a declared bound, signed elimination for
S-invariants, a `revl analyze` subcommand across `cli/parser.py` and
`__main__.py`, refusal wording, and a false-positive measurement - all of which
currently reports the empty set on the composition IR, and none of which
reaches the process graph where the one real finding is.

---

## 9. Posture: warn by default, refuse per shape

The argument, for the record, because it does not depend on §3 and survives
every trigger in §7.

**revl's gates fail closed because their checks are decidable and exact.** G2
is a dict collision. G3 is a cycle in a finite graph. G7 is a syntactic
property of an `undo` slot. Each of them refuses a program that is *definitely*
wrong, and a refusal that is definitely right is safe to make fatal.

A reachability analysis under a bound is not that. Once T1 or T3 lands and the
search becomes necessary, "no dead state within `k` steps" is a bounded claim,
and its negation is not "there is a deadlock" but "there is a marking with no
enabled transition *in the abstraction*". Every source of imprecision in the
derivation - a `config`-dependent branch, a conditional acquisition, a
capability the net models as always available - turns a legitimate composition
into a refusal. That refusal is unfixable by the author, because the fix is
"convince the analyzer", and the escape hatch would be a suppression comment,
which is how a fail-closed gate becomes decorative.

So:

- **Warn.** A finding is a diagnostic on `revl analyze`, not a `RevlError`.
- **Say the bound.** Item 418's lesson: a check must not claim more than it
  establishes. The output says "no dead state reachable within `k` firings"
  and, when the frontier was truncated, says the search was incomplete - the
  same shape `revl query reaches` already uses for its own APPROX verdict and
  its `assumptions` list (`query.py:680-694`).
- **Never say "maybe deadlock".** A finding is admissible only if the analyzer
  can print the firing sequence that reaches the dead marking and the set of
  transitions that stay disabled in it. A finding without a witness is not
  reported.
- **Promote per-shape, never wholesale.** A *specific* shape whose check is
  exact may become a refusal on its own. §8.1's process cycle is one: a finite
  derived graph, a DFS, no abstraction, and no way for the author to be right
  and the check wrong. The unproduced place in §6 is deliberately NOT one,
  because the ambient case makes it inexact.

The item's own instinct here is right and this note only sharpens it: "this
likely starts as `revl analyze` and only becomes a gate once the false-positive
rate is measured." The refinement is that the granularity of that promotion is
the SHAPE, not the tool. Waiting for a whole analyzer's false-positive rate
before refusing anything would leave §8.1's cycle - which has no false
positives to measure - as a warning for no reason.

---

## 10. Exit tests

The bar is that the analyzer **finds a real deadlock**, not that it runs.

The composition IR cannot meet that bar, and demonstrating *that* is tests 1
through 11. The process graph DOES meet it, and test 12 is the deadlock: a
placement that admits every structural gate and cannot boot. Tests 15 onward
fire when a §7 trigger lands.

**Landed with this note** (`tests/test_438_liveness_shapes.py`, 15 tests):

1. **The mutual wait is refused, by name.** The G3 message names both
   components, the cycle, and the key on each hop.
2. **The starved fan-in is refused within one activation**, citing rule 3.1.
3. **The honest fan-in admits**, so test 2 bites on the starvation and not on
   `merge`.
4. **A stream cannot cross a component boundary** in any of the three
   spellings the surface offers, so shape two has no spelling.
5. **Every place has exactly one producing transition** (P1).
6. **The incidence matrix's only S-invariants are the unproduced places** (C3).
7. **The net is monotone and persistent** (P2, P3) over every reachable
   marking.
8. **`loadOrder` is a witness firing sequence** (P4).
9. **Exhaustive marking-graph search finds one terminal marking and zero dead
   states**, and every maximal permutation converges on it (C1, §3.2).
10. **An unprovided place admits and leaves a permanently dead transition**
    (§6).
11. **That dead transition is already named by `query reaches`**, which
    downgrades its own claim because of it, and the withdrawal cascade is a
    linear query.

**The true positive** (§5.2), which is the test that proves a real wait cycle
is reachable:

12. **A process partition closes a cycle the component DAG does not.** Four
    components in two disjoint chains admit: G3 passes and `loadOrder` is
    produced. Split crosswise across two processes, the process graph is
    `A -> B -> A`. Each runner wires every proxy before it serves, so neither
    listens.
13. **The same components placed together are acyclic**, so the partition is
    the whole cause and the refusal must name the placement, not the code.

**What §8.1 must add when it lands:**

14. **The plan-time refusal fires and names both legs.** The crosswise
    placement is refused by `revl plan` before any process is spawned, naming
    both processes, both keys, and the component at each end. A `[remotes.*]`
    provider is excluded from the graph and does not fabricate a cycle. And the
    guard: every wait primitive in §5's table carries a verdict, so adding one
    without a verdict fails.

**When a trigger lands** (§7). These are the tests that would prove the net
engine finds something:

15. **T1's true positive.** Once `Stream[T]` is a provision: component A
    subscribes the stream provided by C, component B `merge`s C's stream with
    D's. The composition is structurally admissible - G2 sees one provider per
    key, G3 sees no cycle, and each `subscribe` is pointwise legal - and B's
    merge is never enabled. The analyzer must report it with the firing
    sequence, and `_admit_stream_operand` must be shown NOT to catch it.
16. **T4's true positive.** A spawn handle reaching a second component, with
    each of two components blocking on the other's spawned instance. The
    analyzer must name the cycle and G3 must be shown not to contain it.
17. **The false-positive floor.** Every composition in `examples/` and `demo/`
    analyzes clean. A single false positive on a shipped example is a
    blocking defect, because it is the whole reason for §9's posture.
18. **The bound is honest.** A composition whose search truncates prints the
    incomplete verdict and does not print a finding, and the same composition
    with a larger bound either finds the witness or still says incomplete.

---

## 11. Slices and cost

**S1. The process-graph cycle check (§8.1). Build this.** One node per process,
one edge per proxy, the DFS `_link` already has, a plan-time refusal, and the
two decisions in §8.1 recorded in the code. It closes a real liveness bug and
needs neither the net nor a subcommand. Small.

**S2. The wait-edge inventory (§8.2). Build this next.** One table plus a guard
test in the file this note lands with, one row per §5 line. Small, and it is
what will surface the next §7 trigger without anyone watching for it.

Everything below waits on a trigger. Slice order for whoever picks it up then.

**A1. The derivation and `revl analyze --json`, warn-only, no search.** Net off
`ir["manifest"]` (twenty lines, already written in the test file), unproduced
places reported, `cli/parser.py` + `__main__.py` handler in the shape
`revl composition` uses on the 426 branch (`cli/parser.py:122`,
`__main__.py:858`, `:933`). Medium. Reports what §6 reports, so it pays for
itself only as scaffolding.

**A2. The marking-graph search, behind the trigger.** Bounded BFS with the
witness requirement from §9, and the honest-bound wording. Only worth building
once P2 or P3 is false, because until then it enumerates a lattice to say
nothing.

**A3. S-invariants.** Only once P1 is false (T3). Until a place can hold more
than one token, boundedness is free and signed elimination returns §3's trivial
answer.

**Explicitly not in 438, and filed:** the intra-body wait analysis (seam calls,
`block`-policy backpressure, host-code locks) is a different net over a
different IR, and `docs/seam-deadlines.md` is the shipped runtime answer for
that tier. Do not fold it into this item silently; it is where the real hangs
are, and it deserves its own number. §5.3's R3 (`local_prereqs` dropping routed
keys) is a separate small bug and belongs on its own issue, not here.

---

## 12. What would change this note's mind

Stated plainly, so both halves of the result are falsifiable.

**The negative half** - that the composition-IR net has nothing to find - would
be overturned by any of:

- A composition on `main` that is structurally admissible and reaches a dead
  state *without* a placement. Not a sketch: a `.rvl` file that compiles and
  wedges. That would falsify C2.
- A wait primitive missing from §5's table.
- A demonstration that the input arc is consuming, not a read arc, for some
  provision kind that already ships.

None of the three was found, and §5's search is the evidence for the second.

**The positive half** - §5.2's process cycle - would be overturned by finding a
check that already refuses it. The search was `placement.py` and
`distribute.py` for a cycle detection over the process graph, and there is
none; the proxy derivation refuses a key no process provides and stops there.
If a plan-time gate elsewhere already catches this, §8.1 is unnecessary and the
finding downgrades to a documentation gap. That is the one claim in this note
most worth a second reader.
