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
* **The kernel-boundary arm is still open, and still costs.** Issue #1265's
  exit asks for one more step after this one: that the undeclared element join
  the `*` arm in `kernel_boundary._undeclared`, so an undeclared reach is no
  longer read as provably disjoint from the kernel, and that
  `test_kernel_boundary_544.py`'s pinned residual flip from documenting the gap
  to asserting its closure. That step is not taken here and the reason is the
  same measurement issue #1223 took, re-run at this head after PR #1292
  declared the shipped tokens: making `_undeclared` true of the `svc:`
  namespace turns **13 tests across 7 files** red (`test_admit_profile.py`,
  `test_composition_confinement.py`, `test_kernel_boundary_544.py`,
  `test_mcp.py`, `test_mcp_ship.py`, `test_per_root_profile_boundaries.py`,
  `test_r2_allowlist_key_binding.py`), and it changes what
  `mcp.session.admit` accepts on every turn whose granted services spell
  `emission` bare. #1292 moved that from 14 to 13 by declaring this
  repository's own compositions; the rest is an operator-facing surface and
  belongs to its own item, with the change to what a turn accepts stated in its
  own right. Note the two questions are not the same question: the fold asks
  *coverage*, where `*` is the wrong element (above), and the kernel asks
  *disjointness*, where it is the right one.
* **The G4 upper bound on a bare method is still absent.** A provider of a
  service whose method declares no token may emit through anything, and nothing
  in this note changes that: `_method_emissions`'s subset check runs only when
  `capabilities is not None`. Declaring the token is the fix and it is
  available today (PR #1292). What this note closes is the separate question of
  what the *fold* does with a method that never declares one.
