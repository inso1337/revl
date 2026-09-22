# 561. What a bare `emission` names in the attenuation fold

Roadmap item 561, issue #1265. The composition half is PR #1292; this is the
checker half.

## The question

A service method may spell `emission` with no capability list:

```revl
service Store { emission fn put(k: Str, v: Str) }
```

It declares an effect and declines to say what the effect reaches. Issue #1265
asks what the checker should make of that, and names three candidates: refuse
it, resolve it to the unnameable `*`, or keep admitting it with the allowance
made visible.

The answer this note records is a fourth, and it comes out of measuring the
first three rather than choosing among them: **the element is the service the
method is declared on**, because that is the only name the boundary itself
owns. What the fold used instead was the *consumer's* local `requires` key,
which is a name the boundary does not have.

## What the fold compared before

Item 294 moved the attenuation fold off the wiring key and onto the declared
capability token, for a reason `lower._cap_keyed` still states: two components
wire the same boundary under whatever key each likes, so comparing keys
compared two identifiers that name nothing in common, and renaming a child's
`requires` key was enough to launder a boundary past the invariant.
`tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl` is that hole's
reproducer, and it is refused.

A method that declares no token has no declared token to move onto, so that
case was left on the key, in a reserved `key:` namespace
(`lower._wire_cap`). The namespace stops a key from being *mistaken* for a
declared token. It does not stop two different boundaries from being mistaken
for each other, and that is what happens:

```revl reject G4
service Net { emission fn call(u: Str) -> Int }
service Kv  { emission fn put(k: Str) -> Int }
service Task { emission fn go() -> Int }

component Worker requires net: Kv provides task: Task {
  provide task { fn go() { emit net.put("x") return 0 } }
}
component Boss requires net: Net {
  let w = effect spawn Worker with { } undo w.dispose()
}
```

`Boss` holds `Net`. `Worker` reaches `Kv`. Both spell the key `net`, the fold
saw `key:net` on both sides, and the spawn was admitted. Spell the same two
services `emission[netcap]` and `emission[kvcap]` and the identical program is
refused under G4. The declaration spelling, not the program, decided the
verdict.

This is the same corner as the `g4_spawn_widens_capability_same_key` fixture,
one declaration weaker, and it is the spelling the corpus actually uses: of the
556 emission methods in the tree's `.rvl` files, **459 are bare and 97 declare
a token** (measured at this branch's head; PR #1292 declared the 97).

## The three candidates, measured

The three cases below are the whole decision. `laundered` is the program above;
`two keys` is one service required under two different keys by parent and
child; `one key` is the ordinary case. The reference column is the same three
programs with the services' tokens declared, which is what the checker already
does and therefore what the undeclared case has to agree with.

| arm | laundered | two keys | one key |
|---|---|---|---|
| declared tokens (the reference) | REFUSE | admit | admit |
| `key:` + the local wiring key (before) | **admit** | **REFUSE** | admit |
| the unnameable `*` | **admit** | admit | admit |
| the service (this note) | REFUSE | admit | admit |

Pinned in `tests/test_1265_undeclared_emission_boundary.py`, which runs all
three arms against all three programs rather than asserting the table. The same
module pins the arm's non-vacuity on one source: `laundered` with both services
bare is refused, and the same file with one token declared on both is admitted,
so the element decides a verdict rather than sitting inert beside one and it is
not refusing every undeclared program.

**Refusing bare `emission` outright** is not costed at one number, it is costed
at a census: 459 declarations across 339 `.rvl` files. 296 of them are in
`bench/results`, which are recorded model outputs. A recorded output is
evidence about what a model wrote against the language as it was; editing one
destroys the thing it records, and refusing to compile one destroys the
benchmark. 37 more are under `backends/` (codegen scenarios and per-backend
fixtures behind golden sets) and 5 are attested published registry components
(PR #1292 §4 has the provenance). The arm is not reachable from here, whatever
its merits.

**Resolving it to the unnameable `*`** is the arm issue #1265's own exit names,
and on this tree it is a *regression*, not a tightening. `cap_order.covers`
gives `*` one rule ("`*` is strictly top of the whole order and covered only
by `*`"), so a held `*` covers a reached `*`. Resolving both sides of a bare
emission to `*` therefore admits the laundering instead of refusing it, and
admits the two-keys case as well: every one of the three programs compiles.
The arm bites only where the question asked of `*` is *disjointness*
(`cap_order.disjoint` returns False for every pair touching `*`, which is why
`*` is the right element for a kernel-intersection fold, issue #1223). The
attenuation fold asks coverage, not disjointness, and under coverage `*` is not
the fail-closed element it reads as.

That is worth stating plainly because it is the measurement that changed the
answer: the issue's premise, that `*` is "covered by nothing", is true of
`disjoint` and false of `covers`.

## The decision

`lower._undeclared_cap` names the element by the **service** the method is
declared on, in the reserved `svc:` namespace. The namespace is kept for its
original reason: a capability token is a dotted identifier in the grammar, so
a `:` is unspellable in source and a derived element can never collide with a
declared boundary.

The service is composition-independent, so the two sides of a fold agree
exactly when they name the same declaration. That is already what the declared
case means by "the same boundary": a parent requiring `store: S` and a child
requiring `db: S` hold and reach the same token today, because the token lives
on `S`, not on either key. Extending it to the undeclared case is the same
rule, not a new one, which is why the arm reproduces the reference column
exactly.

Two consequences, and the second is the one to be honest about:

* the laundering is refused, naming the two services;
* the two-keys case is **admitted where it used to be refused**. That is a
  widening of what compiles. It is the correct verdict, since the declared case
  has always admitted it, but it is a fail-open direction and so it was
  measured rather than argued. Every `.rvl` file in the tree was compiled under
  both spellings of the element: **1065 files before, 1066 after** (the new
  corpus fixture), and **0 verdicts moved in either direction**, admit to
  refuse or refuse to admit. The widening admits nothing this corpus was
  refusing, and the tightening refuses nothing it was admitting; the two rules
  are reachable from the language and not from any file on the tree, which is
  why the fixture is written by hand. The formal differential agrees over the
  same corpus: 5488 verdicts compared, 5488 agree, 0 mismatches.

## What does not change

* **The G4 provide-method bound.** `docs/capabilities.md` §3 says a crossing
  through key `cache` contributes `cache`, not the callee's `db`, and that
  column still reads the wiring key (`boundsOfDecls` in the formal layer,
  `_check_provider_capability_bound` in the reference). The two columns are
  different questions and item 294 separated them on purpose.
* **G6 confinement**, which reads the key namespace for the same reason.
* **The G8 audit surface.** A bare `emission` still renders `["*"]` in
  `revl audit`: the declaration still says nothing, and saying so is honest.
  Declaring the token is what changes it, which is PR #1292's half.
* **`cap_order`.** No change to `covers`, `disjoint` or the parse. The element
  moved; the order did not.
* **The IR.** No field changes. `manifest.instances` renders the new element
  through `_cap_render`, so a bare crossing's `holds`/`granted` entry reads as
  the service name rather than the wiring key. No committed golden carries
  those lists.

## The formal layer moves with it

`formal/RevL/Theorems/CapCeilings.lean` derives the same two columns under two
`Namer`s, and its `capsOfDecls` had the same `wireCap k` fallback. Leaving it
would be the failure mode `tests/test_formal_attenuation_namespace.py` exists
to prevent: a model that agrees with an implementation defect reports nothing,
because agreement looks like success. `Namer` now receives the wiring key *and*
the service; the capability column reads the service and the bound column reads
the key, which is the same split one argument wider.
`formal/harness/diff_corpus.py` mirrors it, and
`tests/formal_corpus/g4_spawn_widens_undeclared_emission_same_key.rvl` is the
fourth corner of that fixture family: one key, two services, no declaration.

## Residuals

* **Two providers of one service reaching two boundaries.** The element is per
  service, so a service whose providers cross different boundaries contributes
  one name for both. `examples/tenant_attenuation.rvl`'s `Worker.tenant` is
  that shape, and PR #1292 §2 declined to declare a token there for the same
  reason: the only service-level answer is wider than either provider. This arm
  does not fix that and does not make it worse; nothing requires `worker:
  Worker` there, so no undeclared element is produced.
* **`stdlib/server.rvl`'s per-row `Server`.** Its provider is synthesized per
  `host` row and binds an extern named from the row's label, so two rows are two
  boundaries behind one service name. The element conflates them, exactly as the
  wiring key did and exactly as `*` would. Naming them apart means changing what
  the synthesizer spells, which is its own item.
* **The kernel-boundary arm is closed** (issue #1265's exit step, below).
  What it cost was not what this note predicted, and the gap is worth reading:
  the number here was measured against the wrong rule.
* **The G4 upper bound on a bare method is still absent.** A provider of a
  service whose method declares no token may emit through anything, and nothing
  in this note changes that: `_method_emissions`'s subset check runs only when
  `capabilities is not None`. Declaring the token is the fix and it is
  available today (PR #1292). What this note closes is the separate question of
  what the *fold* does with a method that never declares one.


## The kernel arm, and why its cost was mismeasured

Issue #1265's exit asks for one more step after the fold: that the undeclared
element join the `*` arm in `kernel_boundary._undeclared`, so an undeclared
reach is no longer read as provably disjoint from the kernel. This note
deferred it on a measurement - 13 tests across 7 files, re-run from item 545's
14 after PR #1292 declared this repository's own compositions - and called the
remainder an operator-facing surface belonging to its own item.

Re-measured at `32db56d9` the same experiment gives **17 tests across the same
7 files**, not 13. It went UP, and the direction is the useful part: `main`
grew four more tests that depend on the arm not firing. But the number was
never the cost of the rule, because the rule it measures is not the rule the
exit asks for.

### The namespace carries two facts

`_held_capabilities_pairs` builds a `svc:` element on two different occasions:

| occasion | what it means | is the reach undeclared? |
|---|---|---|
| a required service with a bare `emission` method | an effect is declared, its reach is not | **yes** |
| a required service with NO emission method at all | there is no effect | **no - the opposite** |

The second exists only so the coverage fold has a boundary identity to compare
(the docstring says so: "a child cannot reach a non-emission service"). It is a
proof that the wiring reaches nothing, and the tree enforces it: a provider of
a plain `fn` that emits is refused under G4 with `` `Kv.get` is declared plain,
but this implementation reaches `fs.write` ``.

Once both are `Cap`s they are the same token shape, so a predicate that reads
the namespace alone cannot tell them apart. Making `_undeclared` true of the
whole `svc:` namespace therefore refuses a candidate that composes only **pure**
services, which is the ordinary admitted turn. That is what 16 of the 17 reds
are. The seventeenth is the pinned residual itself.

### The rule the exit actually asks for

Answer the question where the declarations are. `lower._undeclared_emission_
services` names the services with at least one `emission` method and no
capability token; `_check_kernel_boundary` turns those into elements and passes
them to `kernel_boundary.offending`, which keeps the judgement (an undeclared
reach is not provably disjoint from the kernel) and stops inferring the fact
from a token that never carried it.

Measured the same way at the same head: **1 test across 1 file**, and it is
`test_the_key_namespaced_residual_is_still_admitted`, the pin whose whole job
was to flip; it does, as `test_the_undeclared_service_element_is_refused`. The
other 150 tests in those 7 files pass unchanged and none is edited: the
baseline there is 151 passed / 8 skipped, this branch 156 / 8, the five extra
being new controls.

The distinction this note drew between the two folds holds, and it is why the
arm is takeable here rather than deferrable. The fold asks **coverage**, where
`*` is the wrong element: `cap_order.covers(*, *)` is True, so resolving both
sides of an undeclared emission to `*` admits the laundering. The kernel asks
**disjointness**, where `*` is the right one: `cap_order.disjoint(*, x)` is
False for every `x`. Both columns are pinned in
`test_the_two_predicates_disagree_about_the_unnameable_element`, because the
proposal that made `*` the fold element was argued from "covered by nothing"
and that sentence is true of one predicate and false of the other.

### Why a bare `emission` is not provably disjoint from the kernel

Not by analogy with `*`, and not because "undeclared" sounds like "unbounded".
Because the declaration bounds nothing, and that is measurable on this tree:
`_method_emissions` runs its subset check only when a token is declared, so

```revl
service Kv { emission fn put(k: Str) -> Str }
component KvProvider requires fs: Fs provides kv: Kv {
  provide kv { fn put(k) { emit fs.write("x") return "v" } }
}
```

compiles. A candidate holding `kv: Kv` reaches whatever that provider reaches,
and nothing in the composition states that it stops short of the kernel. Give
`Kv.put` a token and the same candidate admits, because the reach is now a name
the kernel's set can be compared against. One source, two declarations,
opposite verdicts.

### What stops being accepted

Scoped to `undeclared_reaches_kernel`, which is the untrusted-author profile
and nothing else: `mcp.session.admit` and `revl_admit`, a non-first-party
composition row, and the gate's candidate compile. The first-party tree is the
subject of the loop rather than a candidate passing through admission, so its
459 bare emission methods are untouched.

A per-turn candidate is refused under `G8` when it wires a granted service
whose `emission` method names no capability token. It is the candidate's
`requires` that decides and not what it provides, and the declaration that
decides may be the operator's rather than the candidate's: a turn does not
redeclare the running composition's services, and the manifest's declarations
are in the same table the fold reads
(`test_the_granted_service_may_be_declared_only_in_the_running_manifest`).

Three fixes, and an operator can take the third without owning the service:
declare the token on the service's emission methods, drop the wiring, or stop
granting that service to untrusted candidates.

On this tree the granted-side services that spell `emission` bare are
`stdlib/server.rvl`'s `Server` (`get`, `post`, `put`, `patch`, `delete`,
`head`) and the registry's `Database.execute` and `Cache.put`
(`audited_database`, `mysql_database`, `pg_database`, `user_cache`). Neither is
repaired here, and neither is an oversight. PR #1292 stated why it declined
both: the registry components are published release artifacts whose source
bytes are attested, so editing one in place is a release rather than a
refactor and they should carry tokens through a version bump; and `Server`'s
provider is synthesized per `host` row with the extern named from the row's
label, so no fixed token is true for every composition and making one true
means changing what the synthesizer spells. An operator granting either to a
per-turn candidate takes the third fix until those land.
