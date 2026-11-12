import RevL.Typing

/-!
# G1Coverage: the `G1` **coverage** row (issue #2744)

`formal/STATUS.md` carried G1 as `partial` for one reason that could be
worked: `RevL.G1.declared_only_access` says an undeclared access cannot be
*written*, and since issue #1807 `RevL.G1Access` decides the rule over each
component's access roots on the corpus (`GA` rows, the `G1` verdict, 21
`agree-G1` documents). Neither says that the checker's name resolution
VISITS the statements of a real component body. A body statement the walk
never lowered is a statement whose undeclared reads are never refused, and
the corpus row would agree about it vacuously.

G9 had the same shape of gap and closed it over `RevL.Syntax.Body`
(issue #2108, `RevL.G9Coverage.coversBodyB`, the `GB`/`GP`/`GW`/`GC` rows).
This file states G1's coverage obligation the same way, over the same
`Body`: every scope a body induces is opened by the access walk, as many of
its statements are visited as the scope declares, and every declared parameter
is seeded into the scope's own environment, so the per-scope classification the rule will
later compare against has something to be per-scope about.

## The row, and the route it takes

G1 is enforced by name resolution DURING lowering
(`lower.py`: "G1 undeclared access -> `_lower_postfix` name resolution"), so
the checker has no separate walk function to wrap the way the G9 row wraps
`taint._walk_component_methods`. The walk IS the lowering: every activation
statement passes through `_lower_component`'s `_dispatch_action`, and every
provide-method statement through `_lower_provide`'s body loop. The harness
therefore observes the walk at those two sites
(`lower._ACCESS_WALK`, installed for the duration of one
`compile_files` call) and records, per scope, the scope's label, how many
statements the walk visited there, and the parameters it seeded when it
opened the scope.

* the body comes from the PARSE (`G1B` rows: each scope's label, statement
  count and declared parameters);
* the walk comes from the CHECKER (`G1W` rows: each scope the walk actually
  opened, how many statements it visited and the parameters it seeded);
* `coversAccessBodyB` below decides whether the walk covers the body;
* the oracle prints `G1C <file> <comp> <scope> coverage=ok|fail` and the
  differential harness compares it against the model.

**The visiting half can only be as good as the exporter's observation of the
walk.** No Lean theorem can prove that the Python recorder is installed, that
it fires on every statement, or that the two sites above are still the sites
the checker lowers through. What is proved here is that the DECIDER's meaning
is exactly the three conjuncts below and that each conjunct is a distinct way
for a walk to fail; what the harness adds is an independent observation and a
ratchet (`g1coverage_coverage`) that must flip a verdict under each of four
shortenings, so a constant-true row cannot pass the gate. The harness keeps a
separate fatal bucket (`missed-G1-coverage-observation`) for a walk it failed
to observe at all, and its ratchet names a wholly dead call site as an
instrument failure rather than as a checker regression.

**Two limits of the observation, stated rather than smuggled.**

* The count is of a scope's **top-level statements**, the list `Body.scopes`
  carries, which is the flat `Stmt` list L0 can express. A statement inside an
  `if`/`while`/`for` body is lowered by the scope's own recursion and is not
  counted as a second statement. A lowering bug that skips a NESTED statement
  is therefore outside this row (the G9 row has the same granularity); what
  the row catches is a skipped scope or a skipped top-level statement, which
  is the class of bug the activation-skip failure belonged to.
* The walk's scopes are compared in `Body.scopes` order: the activation
  scope first when the walk visited an activation statement, then the
  provide scopes **in the order the checker opened them**. The checker's own
  opening order differs from that on some admitted bodies: it refuses an
  `effect`/`let-effect`/`timer` after a `provide` block but accepts an `emit`
  or an `if` there, and the lowering loop then opens the provide scope before
  it visits that activation statement. The harness therefore moves the
  activation scope to the front, the one reordering it applies, and the
  provide scopes keep the walk's order, so the label conjunct still reads
  them as walked.

## The three conjuncts, and why each one is here

* the scope's LABEL, a walk that never opened a provide method's scope, or
  that reports a scope the body does not have, fails here;
* the scope's STATEMENT COUNT, a walk that opened the scope and then lowered
  only some of its statements fails here. This is the general form of the
  historical activation-skip class of bug (a walk that stops early);
* the scope's DECLARED PARAMETERS, a walk that seeded fewer (or other)
  parameters than the method declares would classify that method's locals
  against the wrong environment. This is the observation the second slice of
  the issue moves the root classification onto: the exporter's classification
  is component-wide today, and the parameters are the per-scope fact the
  checker already knows.

## What this row is NOT

**Not the rule.** That is `RevL.G1Access` (the `G1` verdict), which decides
that every access root is a declared requirement. This row decides nothing
about a root; it decides whether the walk that resolves roots visited the
body the parse enumerates.

**Not per-scope classification yet.** The exporter still classifies a root as
local, callable, import, host family or constructor component-wide. That is
the issue's second slice; this file's parameter conjunct is its observable
precondition, and the row says so rather than implying the classification is
already compared.

**Not a proof about Python.** The independence of the two observations is a
property of the harness, and the harness is what carries it.
-/

namespace RevL.Syntax

/-- The parameters a scope declares, by name. For G1 the qualifier is NOT part
of the observation: every declared parameter is a local of its own scope
whatever its origin, which is exactly the per-scope fact a component-wide
classification cannot express. -/
def Scope.paramNames (s : Scope) : List String := s.params.map (·.name)

/-- A body's declared parameter names, by scope, read through
`Body.scopeParams`, the projection the export and the oracle both carry. -/
def Body.scopeParamNames (b : Body) : List (List String) :=
  b.scopeParams.map fun lp => lp.2.map (·.name)

end RevL.Syntax

namespace RevL.G1Coverage

open RevL.Syntax RevL.Typing

/-! ## The row's vocabulary -/

/-- One scope as the checker's lowering walk reports it: the scope's label,
how many statements of that scope the walk visited, and the declared parameter
names the walk seeded into the scope's own environment, in declaration
order. -/
structure Walked where
  /-- The scope's label: `activation`, or `<provide key>.<method>`. -/
  scope : String
  /-- How many statements of the scope the walk visited. -/
  stmts : Nat
  /-- The parameter names the walk seeded into the scope's own environment,
  in the order it seeded them. -/
  params : List String
  deriving Repr, BEq, DecidableEq

/-- One scope of the walk against one scope of the body: the same label, as
many visited statements as the scope has, and the same declared parameters.
Each `decide` is one of the three conjuncts, kept separate so a failure names
which one moved. -/
def Walked.matches (x : Walked) (s : Scope) : Bool :=
  decide (x.scope = s.label) && decide (x.stmts = s.stmts.length)
    && decide (x.params = s.paramNames)

/-- The walk's labels, in order. -/
def walkLabels (w : List Walked) : List String := w.map (·.scope)

/-- The walk's visited-statement counts, in order. -/
def walkCounts (w : List Walked) : List Nat := w.map (·.stmts)

/-- The walk's seeded parameters, in order. -/
def walkParams (w : List Walked) : List (List String) := w.map (·.params)

/-- The scopes' labels, in order. -/
def scopeLabels (ss : List Scope) : List String := ss.map (·.label)

/-- The scopes' statement counts, in order. -/
def scopeCounts (ss : List Scope) : List Nat := ss.map (fun s => s.stmts.length)

/-- The scopes' declared parameter names, in order. -/
def scopeParamNames (ss : List Scope) : List (List String) :=
  ss.map Scope.paramNames

/-! ## The deciders -/

/-- The walk covers the scopes: scope by scope, in order, the walk's label,
visited-statement count and seeded parameters all match. `false`, never
vacuously `true`, when either list runs out, which is what makes a walk that
never opened a scope a failure rather than a prefix that happens to agree. -/
def coversAccessB : List Walked → List Scope → Bool
  | [], [] => true
  | x :: xs, s :: ss => x.matches s && coversAccessB xs ss
  | _, _ => false

/-- The coverage obligation on a whole component body: the access walk covers
the body's `Body.scopes`, the activation first (when it has a statement),
then one scope per provide method, in declaration order. -/
def coversAccessBodyB (w : List Walked) (b : Body) : Bool := coversAccessB w b.scopes

/-- The walk the model says a checker must perform on a body: each scope of
`Body.scopes` opened with that scope's statement count and its declared
parameters. `coversAccessB` is what the oracle decides on the walk the REAL
checker performed, so the two are compared on the same columns. -/
def walkOf (b : Body) : List Walked :=
  b.scopes.map fun s =>
    { scope := s.label, stmts := s.stmts.length, params := s.paramNames }

/-- `coversAccessB` is exactly the conjunction of the three conjuncts, the
walk's labels, its counts and its seeded parameters, against the scopes'. -/
theorem coversAccessB_iff (w : List Walked) (ss : List Scope) :
    coversAccessB w ss = true ↔
      walkLabels w = scopeLabels ss
        ∧ walkCounts w = scopeCounts ss
        ∧ walkParams w = scopeParamNames ss := by
  induction w generalizing ss with
  | nil =>
      cases ss with
      | nil =>
          simp [coversAccessB, walkLabels, scopeLabels, walkCounts,
            scopeCounts, walkParams, scopeParamNames]
      | cons s ss =>
          refine ⟨fun h => by simp [coversAccessB] at h,
            fun h => absurd h.1 (by simp [walkLabels, scopeLabels])⟩
  | cons x xs ih =>
      cases ss with
      | nil =>
          refine ⟨fun h => by simp [coversAccessB] at h,
            fun h => absurd h.1 (by simp [walkLabels, scopeLabels])⟩
      | cons s ss =>
          simp only [coversAccessB, Walked.matches, Bool.and_eq_true,
            decide_eq_true_iff]
          rw [ih ss]
          simp only [walkLabels, scopeLabels, walkCounts, scopeCounts,
            walkParams, scopeParamNames, List.map_cons, List.cons.injEq]
          constructor
          · rintro ⟨⟨⟨hA, hB⟩, hC⟩, h1, h2, h3⟩
            exact ⟨⟨hA, h1⟩, ⟨hB, h2⟩, hC, h3⟩
          · rintro ⟨⟨hA, h1⟩, ⟨hB, h2⟩, hC, h3⟩
            exact ⟨⟨⟨hA, hB⟩, hC⟩, h1, h2, h3⟩

/-- `Body.scopeParamNames` is the same list `Body.scopes` induces, which is
what lets `coversAccessBodyB_iff` read the parameter conjunct through the
projection the export carries. -/
theorem scopeParamNames_eq (b : Body) :
    b.scopes.map Scope.paramNames = b.scopeParamNames := by
  unfold RevL.Syntax.Body.scopeParamNames RevL.Syntax.Body.scopeParams
    RevL.Syntax.Scope.paramNames
  rw [List.map_map]
  rfl

/-- The same, read on a whole body through the two body projections the export
carries: `Body.scopeParams` for the parameters and `Body.scopes` for the
statement counts. This is the form the guarantee table cites. -/
theorem coversAccessBodyB_iff (w : List Walked) (b : Body) :
    coversAccessBodyB w b = true ↔
      walkLabels w = b.scopeLabels
        ∧ walkCounts w = b.scopes.map (fun s => s.stmts.length)
        ∧ walkParams w = b.scopeParamNames := by
  rw [coversAccessBodyB, coversAccessB_iff, Body.scopeLabels]
  have h : scopeParamNames b.scopes = b.scopeParamNames := scopeParamNames_eq b
  rw [h]
  constructor <;> intro h <;> exact h

/-- The model's prescribed walk covers any scope list, by construction. -/
theorem coversAccessB_self (ss : List Scope) :
    coversAccessB (ss.map fun s =>
      ({ scope := s.label, stmts := s.stmts.length, params := s.paramNames }
        : Walked)) ss = true := by
  induction ss with
  | nil => rfl
  | cons s ss ih =>
      simp only [List.map_cons, coversAccessB, Walked.matches,
        Bool.and_eq_true, decide_eq_true_iff, true_and, and_self]
      exact ih

/-- **The coverage obligation, satisfied.** The walk the model prescribes for a
body covers that body. Without this the refusal witnesses below would be
satisfied by a decider that refused everything. -/
theorem walk_covers (b : Body) : coversAccessBodyB (walkOf b) b = true := by
  unfold walkOf coversAccessBodyB
  exact coversAccessB_self b.scopes

/-! ## Non-vacuity

The witness body is the same shape the G9 row uses: an activation of two
statements and one `provide Store` block whose `get` method declares four
parameters spanning every qualifier, with a two-statement body. Two
statements per scope is what makes a SHORTENED walk distinguishable from a
complete one, and the four qualifiers are what show that for access the
qualifier is not part of the observation: all four are locals of this scope. -/

/-- A `Secret[T]` parameter: a local of its scope like any other. -/
def secretParam : Param := ⟨"payload", .secret⟩

/-- An `Untrusted[T]` parameter. -/
def untrustedParam : Param := ⟨"probe", .untrusted⟩

/-- A `Trusted[T]` parameter. -/
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

/-- Every declared parameter is a local of its scope, whatever its qualifier:
the observation the component-wide classification cannot make per scope. -/
theorem scope_param_names_keep_every_qualifier :
    (⟨"Store.get", [], [secretParam, plainParam, untrustedParam,
       trustedParam]⟩ : Scope).paramNames
      = ["payload", "count", "probe", "cert"] := by decide

/-- The witness body's scope enumeration: the activation first, with its two
statements and no parameters, then the `Store.get` scope with its two
statements and all four declared parameters. -/
theorem witnessBody_scopes :
    witnessBody.scopeLabels = ["activation", "Store.get"]
      ∧ witnessBody.scopes.map (fun s => s.stmts.length) = [2, 2]
      ∧ witnessBody.scopeParamNames = [[], ["payload", "count", "probe",
          "cert"]]
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

/-- The model's own walk on the witness body, computed. -/
theorem walkOf_is_the_walk :
    walkOf witnessBody
      = [⟨"activation", 2, []⟩,
         ⟨"Store.get", 2, ["payload", "count", "probe", "cert"]⟩] := by
  decide

/-- The complete walk is admitted, the positive half, on concrete data, so the
refusals below are not the only thing the decider can do. -/
theorem coversAccessB_admits_the_full_walk :
    coversAccessBodyB (walkOf witnessBody) witnessBody = true
      ∧ coversAccessBodyB (walkOf activationlessBody) activationlessBody
          = true := by
  refine ⟨?_, ?_⟩ <;> decide

/-- **The activation-skip bug, as a refusal.** A walk that never opened the
activation scope is refused even though it reports the provide scope
completely and correctly. -/
theorem coversAccessB_refuses_a_walk_that_drops_the_activation_scope :
    coversAccessBodyB
      [⟨"Store.get", 2, ["payload", "count", "probe", "cert"]⟩] witnessBody
      = false := by decide

/-- **The witness the issue demands: a SHORTENED walk.** The same walk with the
activation scope present but reporting one statement instead of two is refused.
This is the obligation biting when the walk stops early. -/
theorem coversAccessB_refuses_a_shortened_walk :
    coversAccessBodyB
      [⟨"activation", 1, []⟩,
       ⟨"Store.get", 2, ["payload", "count", "probe", "cert"]⟩]
      witnessBody = false := by decide

/-- A walk that opened the provide scope but seeded fewer parameters than the
method declares, the shape that would classify that method's locals against
another scope's environment, is refused. -/
theorem coversAccessB_refuses_a_walk_that_strips_a_param :
    coversAccessBodyB
      [⟨"activation", 2, []⟩,
       ⟨"Store.get", 2, ["payload"]⟩]
      witnessBody = false := by decide

/-- …and the other direction: a walk that seeds a parameter the scope does not
declare is refused too, so the parameter conjunct is a comparison and not a
lower bound. -/
theorem coversAccessB_refuses_a_walk_that_seeds_an_extra_param :
    coversAccessBodyB
      [⟨"activation", 2, []⟩,
       ⟨"Store.get", 2, ["payload", "count", "probe", "cert", "extra"]⟩]
      witnessBody = false := by decide

/-- A walk that reports a scope the body does not have is refused: on a body
whose activation is empty there is no activation scope to open. -/
theorem coversAccessB_refuses_a_scope_the_body_does_not_have :
    coversAccessBodyB
      [⟨"activation", 0, []⟩,
       ⟨"Store.get", 2, ["payload", "count", "probe", "cert"]⟩]
      activationlessBody = false := by decide

/-- **Non-vacuity**: the row admits the complete walk on both witness bodies and
refuses each way a walk can be wrong, a missing scope, a shortened scope, a
stripped parameter, an extra parameter and a scope the body does not have, so
a decider that answered constantly, in either direction, fails here. -/
theorem g1Coverage_not_vacuous :
    coversAccessBodyB (walkOf witnessBody) witnessBody = true
      ∧ coversAccessBodyB (walkOf activationlessBody) activationlessBody
          = true
      ∧ coversAccessBodyB
          [⟨"Store.get", 2, ["payload", "count", "probe", "cert"]⟩]
          witnessBody = false
      ∧ coversAccessBodyB
          [⟨"activation", 1, []⟩,
           ⟨"Store.get", 2, ["payload", "count", "probe", "cert"]⟩]
          witnessBody = false
      ∧ coversAccessBodyB
          [⟨"activation", 2, []⟩, ⟨"Store.get", 2, ["payload"]⟩]
          witnessBody = false
      ∧ coversAccessBodyB
          [⟨"activation", 2, []⟩,
           ⟨"Store.get", 2, ["payload", "count", "probe", "cert", "extra"]⟩]
          witnessBody = false := by
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_⟩ <;> decide

end RevL.G1Coverage
