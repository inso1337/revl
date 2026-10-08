import RevL.Typing

/-!
# G9Coverage — the `G9`/`G-SECRET-FLOW` **coverage** row (issue #2108)

`formal/STATUS.md` carried G9 *path coverage* as its one row marked
`UNPROVED, unstatable`. The two real checker bugs that motivated the coverage
obligation were in the checker's WALK, not in the rule:

* `_walk_component_methods` skipped the activation body entirely, so a
  component activation body was never taint-checked at all; and
* a declared `Secret[T]` receiver's own body saw a bare value, because
  `_seed_param_env` seeded only the parameters the taint model had a declared
  origin for and the qualifier was stripped at the receiver.

Neither is a fact about the rule — `RevL.G9` and `RevL.Lemmas.Admits` were
already right, and `RevL.G9Flow.g9RowB` already decides them at the sink on
the corpus (issue #1811 groups 2 and 3, PRs #2081/#2082). They are facts about
whether the walk VISITS the statements. And the obligation could not be
*stated* at all, because L0 had no way to write down a component body: `Stmt`
alone is the activation's surface, with no `provide`/activation distinction
and no typed parameters. A calculus that cannot express a provide method's
typed parameters cannot state an obligation about them, which is why the
coverage obligation was carried as UNPROVED and *unstatable* rather than as a
`sorry` on a false statement.

`RevL.Syntax` and `RevL.Typing` now carry the body syntax (`Body`, `Item`,
`Provide`, `Method`, `Param`, `Qual`) and its admission relation. This file
states the obligation over it.

## The row, and the route it takes

Route A of issue #1811 grows L0 so the checker's COVERAGE of the walk is
itself proved (roadmap item 418, step 9). That step's own precondition —
"after an operational semantics exists" — is what this file supplies; it is
not a claim that item 418's step 9 already landed. `docs/v2.0-roadmap.md`
marked item 418 fully landed while `STATUS.md` carried G9 coverage as
UNPROVED; the roadmap marker was the one that over-claimed, and it is
corrected in the same change.

The row itself is a differential row, like every other one in
`formal/harness/Oracle.lean`:

* the exporter (`formal/harness/diff_corpus.py`) reads the body off the PARSE
  (`GB` rows: each scope's label, statement count and origin-carrying
  parameters) and the walk off the CHECKER (`GW` rows: each scope the walk
  actually opened, how many statements it visited there, and the parameters it
  actually seeded into that scope's environment);
* `coversBodyB` below decides whether the walk covers the body;
* the oracle prints `GC <file> <component> <scope> coverage=ok|fail` and the
  differential harness compares it against the model.

The two observations are independent in the only way that matters: `GB` comes
from `Parser(...).parse()` and `GW` comes from `compile_files(...)` running the
real `_FlowChecker` walk, so a walk that stops early moves `GW` and not `GB`.

## What this row is NOT

**Not the rule.** That is `RevL.G9Flow` (the `TAINT` row), which decides
`Admits` at the sink the checker discovered. This row decides nothing about
taint; it decides whether the walk that produced the sink visited the body.

**Not a proof about Python.** No Lean theorem can be. What is proved here is
that the DECIDER's meaning is exactly the three conjuncts below, and that each
conjunct is a distinct way for a walk to fail. The independence of the two
observations is a property of the harness, and the harness carries a ratchet
(`g9coverage_coverage`) that must flip a verdict under each of three
shortenings, so a constant-true row fails the gate.

**Not per-fixture.** The limitation was in L0's expressible syntax, so this is
one structural statement over `Body`, not four statements over the four taint
fixtures.

## The three conjuncts, and which bug each one catches

`coversB` reads exactly three things off a walk, per scope, in the body's
order:

* the scope's LABEL — a walk that never opened the activation scope, or that
  reports a scope the body does not have, fails here;
* the scope's STATEMENT COUNT — a walk that opened the activation scope and
  then visited only some of its statements fails here; and
* the scope's ORIGIN-CARRYING PARAMETERS — a walk that seeded fewer (or more,
  or differently qualified) parameters than the scope declares fails here.

The second is the historical activation-skip bug in its general form; the
third is the historical `Secret[T]`-stripped-at-the-receiver bug. `Trusted[T]`
is deliberately NOT among the origin-carrying qualifiers: the checker records
it as a clean sink and seeds nothing, so a row that treated it as seeding
would demand a seed the checker must not make.

## The private seams this row pins

The exporter reads three private things out of `src/revl/taint.py`, and the
`GW` row's columns are meaningless if any of them moves. They are named here
so a drift is a *loud* failure and not a silent agreement:

* `revl.taint._FlowChecker.run`'s call signature — the walk is observed by
  wrapping `revl.taint._walk_component_methods`, and the recording checker
  subclass is installed only for the duration of that call, so every recorded
  `run` is by construction the real refusal walk over a real component body.
  `_infer_scope_env` and `_infer_signatures` also construct `_FlowChecker`s
  (over `_callables`, with no `endorse_label`), so a filter on the checker's
  constructor arguments would not have been enough;
* `endorse_label`'s spelling — `f"{component} activation"` for the activation
  and `f"{component}.{method}"` for a provide method. The exporter asserts both
  spellings against the component it is walking and HARD-FAILS the run when
  they do not hold; and
* `_seed_param_env`'s behaviour — it seeds `env[name]` for the parameter
  indices `_declared_param_origins` names, and for no others, so the names in
  the outermost `run`'s `env` ARE the walk's parameter observation. `env` is
  also written to during the walk, which is why the exporter snapshots the
  OUTERMOST call per label: `_FlowChecker.run` recurses into nested bodies
  (a `for`/`if` arm) with the SAME `endorse_label`, so only the outermost call
  per label counts as that scope's visit.

-/

namespace RevL.G9Coverage

open RevL.Syntax RevL.Typing

/-! ## The row's vocabulary -/

/-- One scope as the checker's walk reports it: the scope's label, how many
statements of that scope the walk visited, and the parameters the walk seeded
into that scope's own environment.

`params` is `List Param`, not `List String`, because the walk's parameter
observation is not only WHICH parameters were seeded but what the seeded value
WAS — the origin `_seed_param_env` wrote into the environment. The harness maps
that origin back to the qualifier spelling (`input` → `untrusted`,
`confidential` → `secret`, `retained:*` → `retained`) and files a fatal
`missed-G9-coverage-observation` for an origin it does not carry, so a
qualifier downgrade is a failure and not an invisible agreement. -/
structure Walked where
  /-- The scope's label: `activation`, or `<provide key>.<method>`. -/
  scope : String
  /-- How many statements of the scope the walk visited. -/
  stmts : Nat
  /-- The parameters the walk seeded into the scope's own environment, in the
  order it seeded them. -/
  params : List Param
  deriving Repr, BEq, DecidableEq

/-- One scope of the walk against one scope of the body: the same label, as
many visited statements as the scope has, and the same origin-carrying
parameters. Each `decide` is one of the three conjuncts, and the three are
`&&`-ed rather than folded into a single comparison so that a failure names
which one moved. -/
def Walked.matches (x : Walked) (s : Scope) : Bool :=
  decide (x.scope = s.label) && decide (x.stmts = s.stmts.length)
    && decide (x.params = s.origins)

/-- The walk's labels, in order. -/
def walkLabels (w : List Walked) : List String := w.map (·.scope)

/-- The walk's visited-statement counts, in order. -/
def walkCounts (w : List Walked) : List Nat := w.map (·.stmts)

/-- The walk's seeded parameters, in order. -/
def walkParams (w : List Walked) : List (List Param) := w.map (·.params)

/-- The scopes' labels, in order. -/
def scopeLabels (ss : List Scope) : List String := ss.map (·.label)

/-- The scopes' statement counts, in order. -/
def scopeCounts (ss : List Scope) : List Nat := ss.map (fun s => s.stmts.length)

/-- The scopes' origin-carrying parameters, in order. -/
def scopeOrigins (ss : List Scope) : List (List Param) := ss.map Scope.origins

/-! ## The deciders

The four declarations below are `def`s, not theorems, and are deliberately
**not** in `run_gate.sh`'s argv or the non-vacuity registry — no other `def`
in the layer is. A definition is not a claim, so a non-vacuity row for one
would be a name that cannot fail. What is registered is the theorem that pins
each decider (`coversB_iff`, `coversBodyB_iff`, `walkOf_is_the_walk`), and
`#print axioms` reads those transitively through the definitions they unfold,
so a `sorry` hidden in a decider still surfaces in the axioms gate. -/

/-- The walk covers the scopes: scope by scope, in order, the walk's label,
visited-statement count and seeded parameters all match. `false` — never
vacuously `true` — when either list runs out, which is what makes a walk that
never opened a scope a failure rather than a prefix that happens to agree. -/
def coversB : List Walked → List Scope → Bool
  | [], [] => true
  | x :: xs, s :: ss => x.matches s && coversB xs ss
  | _, _ => false

/-- The coverage obligation on a whole component body: the walk covers the
body's `Body.scopes` — the activation first (when it has a statement), then
one scope per provide method, in declaration order. This is the body-level
spelling the guarantee table cites, through its pinning theorem
`coversBodyB_iff`. -/
def coversBodyB (w : List Walked) (b : Body) : Bool := coversB w b.scopes

/-- The walk the model says a checker must perform on a body: it reports each
scope of `Body.scopes` with that scope's statement count and that scope's
origin-carrying parameters. `walk_covers` is the fact that this walk covers the
body; `coversB` is what the oracle decides on the walk the REAL checker
performed, so the two are compared on the same columns. -/
def walkOf (b : Body) : List Walked :=
  b.scopes.map fun s =>
    { scope := s.label, stmts := s.stmts.length, params := s.origins }

/-- `coversB` is exactly the conjunction of the three conjuncts — the walk's
labels, its counts, and its seeded parameters — against the scopes'. Proved by
induction on both lists, because the definition is a recursion and not a single
comparison: the statement is what says a walk that is long where it should be
short (or short where it should be long) is refused, and a definitional
unfolding would not say it. -/
theorem coversB_iff (w : List Walked) (ss : List Scope) :
    coversB w ss = true ↔
      walkLabels w = scopeLabels ss
        ∧ walkCounts w = scopeCounts ss
        ∧ walkParams w = scopeOrigins ss := by
  induction w generalizing ss with
  | nil =>
      cases ss with
      | nil =>
          simp [coversB, walkLabels, scopeLabels, walkCounts, scopeCounts,
            walkParams, scopeOrigins]
      | cons s ss =>
          refine ⟨fun h => by simp [coversB] at h,
            fun h => absurd h.1 (by simp [walkLabels, scopeLabels])⟩
  | cons x xs ih =>
      cases ss with
      | nil =>
          refine ⟨fun h => by simp [coversB] at h,
            fun h => absurd h.1 (by simp [walkLabels, scopeLabels])⟩
      | cons s ss =>
          simp only [coversB, Walked.matches, Bool.and_eq_true, decide_eq_true_iff]
          rw [ih ss]
          simp only [walkLabels, scopeLabels, walkCounts, scopeCounts, walkParams,
            scopeOrigins, List.map_cons, List.cons.injEq]
          constructor
          · rintro ⟨⟨⟨hA, hB⟩, hC⟩, h1, h2, h3⟩
            exact ⟨⟨hA, h1⟩, ⟨hB, h2⟩, hC, h3⟩
          · rintro ⟨⟨hA, h1⟩, ⟨hB, h2⟩, hC, h3⟩
            exact ⟨⟨⟨hA, hB⟩, hC⟩, h1, h2, h3⟩

/-- The same, read on a whole body. This is the form the guarantee table
carries: a `GC` row's `ok` says the walk covered the body, and it says so
because the three conjuncts hold — not because a decider returned `true` for
some other reason. -/
theorem coversBodyB_iff (w : List Walked) (b : Body) :
    coversBodyB w b = true ↔
      walkLabels w = b.scopeLabels
        ∧ walkCounts w = b.scopes.map (fun s => s.stmts.length)
        ∧ walkParams w = b.scopes.map Scope.origins :=
  coversB_iff w b.scopes

/-- The model's prescribed walk covers any scope list, by construction. The
list-level form of `walk_covers`, stated over `List Scope` so the induction is
over the list the walk was built from rather than over a body whose `scopes`
would have to be generalized away. -/
theorem coversB_self (ss : List Scope) :
    coversB (ss.map fun s =>
      ({ scope := s.label, stmts := s.stmts.length, params := s.origins }
        : Walked)) ss = true := by
  induction ss with
  | nil => rfl
  | cons s ss ih =>
      simp only [List.map_cons, coversB, Walked.matches, Bool.and_eq_true,
        decide_eq_true_iff, true_and, and_self]
      exact ih

/-- **The coverage obligation, satisfied.** The walk the model prescribes for a
body covers that body. Without this the three refusal witnesses below would be
satisfied by a decider that refused everything; with it, the obligation is
satisfiable and the refusals are about the walk and not about the decider being
broken. -/
theorem walk_covers (b : Body) : coversBodyB (walkOf b) b = true := by
  unfold walkOf coversBodyB
  exact coversB_self b.scopes

/-! ## Non-vacuity

The witness body is `examples/app/notes.rvl`'s `TrendingRanker` shape reduced
to the two scopes that matter: an activation of two statements, and one
`provide Store` block whose `get` method declares a `Secret[T]` parameter, a
plain one, an `Untrusted[T]` one and a `Trusted[T]` one and has a
two-statement body. Two statements per scope is what makes a SHORTENED walk
distinguishable from a complete one, and the four qualifiers are what make the
origin filter observable in both directions. -/

/-- A `Secret[T]` parameter: seeds `confidential` into the receiver's own
body. -/
def secretParam : Param := ⟨"payload", .secret⟩

/-- An `Untrusted[T]` parameter: seeds its provenance origin. -/
def untrustedParam : Param := ⟨"probe", .untrusted⟩

/-- A `Trusted[T]` parameter: the checker records it as a clean sink and seeds
NOTHING, so it is not an origin-carrying parameter. -/
def trustedParam : Param := ⟨"cert", .trusted⟩

/-- An unqualified parameter. -/
def plainParam : Param := ⟨"count", .plain⟩

/-- A call head the witness context declares. -/
def callSvc : Expr := .call "svc" [.lit "x"]

/-- The declared requirement keys the witness body is admitted under. -/
def witnessCtx : Ctx := ["svc"]

/-- `callSvc` reaches only declared keys. -/
theorem reachIn_callSvc : ReachIn witnessCtx callSvc := by
  refine ReachIn.call _ _ _ ?_ ?_
  · simp [witnessCtx]
  · intro a ha
    simp only [List.mem_singleton] at ha
    subst ha
    exact ReachIn.lit _ _

/-- The witness provide block: key `Store`, one method `get` carrying the four
qualifiers, with a two-statement body. -/
def storeBlock : Provide where
  key := "Store"
  methods :=
    [{ name := "get"
       params := [secretParam, plainParam, untrustedParam, trustedParam]
       body := [.effect callSvc (.lit "c"), .emit callSvc] }]

/-- The witness body: a two-statement activation and the `Store` block. -/
def witnessBody : Body where
  items := [.step (.pure callSvc), .step (.effect callSvc (.lit "b")),
            .provide storeBlock]

/-- A body whose activation has no statement. `Body.scopes` gives it exactly
one scope, so a walk that reports an activation scope for it is reporting a
scope the body does not have. -/
def activationlessBody : Body where
  items := [.provide storeBlock]

/-- The `Trusted[T]` qualifier is not origin-carrying and neither is a plain
parameter, so the witness scope's expected parameter list is the `Secret[T]`
and `Untrusted[T]` pair in declaration order. A row that filtered only `plain`
— or that treated `Trusted[T]` as seeding — fails here. -/
theorem scope_origins_exclude_plain_and_trusted :
    (⟨"Store.get", [], [secretParam, plainParam, untrustedParam, trustedParam]⟩
        : Scope).origins = [secretParam, untrustedParam] := by decide

/-- The witness body's scope enumeration: the activation first, with its two
statements and no parameters, then the `Store.get` scope with its two
statements and its two origin-carrying parameters. -/
theorem witnessBody_scopes :
    witnessBody.scopeLabels = ["activation", "Store.get"]
      ∧ witnessBody.scopes.map (fun s => s.stmts.length) = [2, 2]
      ∧ witnessBody.scopes.map Scope.origins = [[], [secretParam, untrustedParam]]
      ∧ witnessBody.stmts.length = 4 := by
  refine ⟨?_, ?_, ?_, ?_⟩ <;> decide

/-- The witness body is a body the checker ADMITS: every statement of every
item is `TypedIn` under the declared context. Without this the coverage
obligation would be about bodies the checker never walks. -/
theorem witnessBody_is_admitted : Body.TypedIn witnessCtx witnessBody := by
  refine Body.TypedIn.mk _ ?_
  intro i hi
  simp only [witnessBody, List.mem_cons, List.not_mem_nil, or_false] at hi
  rcases hi with rfl | rfl | rfl
  · exact Item.TypedIn.step _ (TypedIn.pure _ _ reachIn_callSvc)
  · exact Item.TypedIn.step _
      (TypedIn.effect _ _ _ reachIn_callSvc (ReachIn.lit _ _))
  · refine Item.TypedIn.provide _ ?_
    intro m hm s hs
    simp only [storeBlock, List.mem_singleton] at hm
    subst hm
    simp only [List.mem_cons, List.not_mem_nil, or_false] at hs
    rcases hs with rfl | rfl
    · exact TypedIn.effect _ _ _ reachIn_callSvc (ReachIn.lit _ _)
    · exact TypedIn.emit _ _ reachIn_callSvc

/-- The model's own walk on the witness body, computed: the activation scope
and the `Store.get` scope, each with two statements, the second carrying the
`Secret[T]` and `Untrusted[T]` parameters and NOT the plain or `Trusted[T]`
ones. -/
theorem walkOf_is_the_walk :
    walkOf witnessBody
      = [⟨"activation", 2, []⟩,
         ⟨"Store.get", 2, [secretParam, untrustedParam]⟩] := by decide

/-- The complete walk is admitted — the positive half, on concrete data, so the
refusals below are not the only thing the decider can do. -/
theorem coversB_admits_the_full_walk :
    coversBodyB (walkOf witnessBody) witnessBody = true
      ∧ coversBodyB (walkOf activationlessBody) activationlessBody = true := by
  refine ⟨?_, ?_⟩ <;> decide

/-- **The activation-skip bug, as a refusal.** A walk that never opened the
activation scope — the `_walk_component_methods` shape before the fix — is
refused even though it reports the provide scope completely and correctly. -/
theorem coversB_refuses_a_walk_that_drops_the_activation_scope :
    coversBodyB [⟨"Store.get", 2, [secretParam, untrustedParam]⟩] witnessBody
      = false := by decide

/-- **The witness the issue demands: a SHORTENED walk.** The same walk with the
activation scope present but reporting one statement instead of two is refused.
This is the obligation biting when the walk stops early, and it is the reason
the row is a statement about the walk's completeness rather than a recount. -/
theorem coversB_refuses_a_shortened_walk :
    coversBodyB
      [⟨"activation", 1, []⟩, ⟨"Store.get", 2, [secretParam, untrustedParam]⟩]
      witnessBody = false := by decide

/-- **The `Secret[T]`-stripped-at-the-receiver bug, as a refusal.** A walk that
seeded the `Untrusted[T]` parameter but not the `Secret[T]` one — a receiver's
own body seeing a bare value — is refused. -/
theorem coversB_refuses_a_walk_that_strips_a_secret_param :
    coversBodyB
      [⟨"activation", 2, []⟩, ⟨"Store.get", 2, [untrustedParam]⟩]
      witnessBody = false := by decide

/-- …and the other direction: a walk that seeds a `Trusted[T]` parameter the
scope does not put an origin on is refused too, so the parameter conjunct is a
comparison and not a lower bound. -/
theorem coversB_refuses_a_walk_that_seeds_a_trusted_param :
    coversBodyB
      [⟨"activation", 2, []⟩,
       ⟨"Store.get", 2, [secretParam, trustedParam, untrustedParam]⟩]
      witnessBody = false := by decide

/-- A walk that reports a scope the body does not have is refused: on a body
whose activation is empty there is no activation scope to open, so reporting
one is a failure rather than an empty scope that happens to agree. -/
theorem coversB_refuses_a_scope_the_body_does_not_have :
    coversBodyB
      [⟨"activation", 0, []⟩, ⟨"Store.get", 2, [secretParam, untrustedParam]⟩]
      activationlessBody = false := by decide

/-- **Non-vacuity**: the row admits the complete walk on both witness bodies and
refuses each of the four ways a walk can be wrong — a missing scope, a
shortened scope, a stripped parameter, an extra parameter — so a decider that
answered constantly, in either direction, fails here. -/
theorem g9Coverage_not_vacuous :
    coversBodyB (walkOf witnessBody) witnessBody = true
      ∧ coversBodyB (walkOf activationlessBody) activationlessBody = true
      ∧ coversBodyB [⟨"Store.get", 2, [secretParam, untrustedParam]⟩]
          witnessBody = false
      ∧ coversBodyB
          [⟨"activation", 1, []⟩, ⟨"Store.get", 2, [secretParam, untrustedParam]⟩]
          witnessBody = false
      ∧ coversBodyB
          [⟨"activation", 2, []⟩, ⟨"Store.get", 2, [untrustedParam]⟩]
          witnessBody = false
      ∧ coversBodyB
          [⟨"activation", 2, []⟩,
           ⟨"Store.get", 2, [secretParam, trustedParam, untrustedParam]⟩]
          witnessBody = false := by
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_⟩ <;> decide

end RevL.G9Coverage
