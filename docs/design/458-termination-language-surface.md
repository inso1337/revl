# 458 (provisional): the termination language surface

Design note. The id 458 is PROVISIONAL and is reconciled at merge; the doc
maps to roadmap item 441 and GitHub issue #120. Design only: no compiler
change, no `src/` change, nothing implemented.

Companion docs: [441-goal-contracts.md](441-goal-contracts.md) (the gate-level
design this builds on), [443-estop.md](443-estop.md),
[442-typed-delegation.md](442-typed-delegation.md),
[246-auto-approve.md](246-auto-approve.md),
[243-witnessed-externs.md](243-witnessed-externs.md),
[teardown-contract.md](teardown-contract.md), [../syntax-2.0.md](../syntax-2.0.md)
§5 and §7.

441-goal-contracts.md settled the controller: four states, a max-fold over a
declared order, one term at each of two shipped chokepoints, a per-turn trail,
no continuation score, no fourth verdict. It answered "new verb, new keyword?"
with "neither" and left the criteria keyed on two wiring-key NAMES, `goal` and
`guard`. This doc is about the part that answer skipped: what in the language
says that a method is a termination criterion, so that the checker has
something to attach its rules to and an agent has no way to write a criterion
the checker does not recognise as one.

---

## 0. The decisions, in one table

| # | Question | Decision | Section |
|---|---|---|---|
| D1 | What is the language surface? | **Two builtin marker types on service operations, `Criterion` and `Guard`.** A service with at least one such operation is a goal service. Nothing new is lexed; the type-head table gains two entries. | §2 |
| D2 | Why a type and not a keyword or a key name? | A key name is a convention the checker cannot see. A keyword touches the lexer, the self-host lexer parity oracle and the grammar summary for one bit of information a type already carries. The service is the right carrier because a service upper-bounds every provider (G4). | §2.2, §2.3 |
| D3 | What does the checker prove? | Six rules, L1 to L6. L1 (a criterion body reaches no emission and no witnessed extern) is the one that matters and it is the existing G5 teardown-slot walker pointed at a new body. | §3 |
| D4 | Where do budgets live? | **Not in the language.** A budget is operator authority, declared at freeze on the MCP and CLI surfaces, recorded in the trail, never in source. The agent may not bound itself. | §4 |
| D5 | Can a per-turn admission add or replace a criterion? | **No.** A turn admitted through item 330's crossing may not provide a goal service. Amendment is `revl_swap` under the item-55 operator authority, as 441 §4 decided. | §3 L3, §5 |
| D6 | Does the self-host compiler need a port? | Yes, in the same slice: one IR key per operation and two type heads. The byte-agreement oracle covers it. | §6 |

---

## 1. The gap, exactly

441 §2 stated the gate-level gap: no gate in revl reads the state of the
world. This doc states the language-level gap underneath it, which is smaller
and can be checked against the tree.

**1.1 Nothing in the compiler knows what a criterion is.** 441 §8 keys the
fold on the wiring keys `goal` and `guard`. Grep `src/revl/lexer.py`,
`parser.py` and `lower.py` for either word as a construct: zero hits. So
`revl check` proves exactly the same things about a contract component as
about any other component. In particular this compiles clean today:

```revl sketch
component Contract requires fs: Fs provides goal: ShipTheFix {
  provide goal {
    fn report_written() = { fs.write("report.md", ""); Some(true) }
  }
}
```

That is 441's C1 violated in the plainest way, a criterion that causes its own
satisfaction, and no diagnostic exists for it because no declaration says the
method is a criterion. 441 put C1 in `ClassMap.classify_call`
(`src/revl/mcp/approval.py:405`), which is the MCP session's per-generation
classifier. It runs at load and swap, in a session, with a policy. It does not
run under `revl check`, and the property is a property of the source.

**1.2 The turn crossing gates crossings, not definitions.** Item 330's
`admit(source, granted)` lands in `Session._wire_turn`
(`src/revl/mcp/session.py:2506`). It rebuilds the class map before plugging,
so a class-(c) crossing through a turn is ticketed like any other. It does not
refuse a turn that PROVIDES a key. A turn providing `goal` is a G2 conflict
only if `goal` is already provided in the same realm; a turn providing a
second, differently named goal service is not a conflict at all, and under a
key-name fold it would simply be ignored, or, under a fold that reads every
`Opt[Bool]` method, it would be read. Either way the agent has a door into the
definition of done, and the door is a name.

**1.3 The self-checking loop stops one level short.** `verified fn` proves
totality (`_check_verified_totality`, `lower.py:1336`), `test` and
`lifecycle test` run assertions over a scratch world, and `assert no_residue`
is the closest thing revl has to "this run ended well". All three are
test-scoped: they say what a body or a scenario does, not when a RUN may
settle. There is no declaration whose meaning is "the composition may commit
when this holds", and no declaration whose meaning is "the composition must
not commit while this holds".

**1.4 The E-Stop's absence of a language surface is the right precedent, for
half the feature.** 443 gave the halt no in-language surface so that a
composition cannot halt itself. 441's C5 wants the same property for the
contract STATE: no `.rvl` body may set it. But the criteria themselves must be
in the language, because the whole point is that they are checked source
rather than prose. The two halves need different treatment and the design
should say so: the criteria get a surface, the state does not.

What is not missing, and is not touched here: the freeze
(`revl_admit` plus `candidate_hash`), the amendment rule (427 F4), the two
chokepoints, the trail, the lattice. All of that is 441's and stands.

---

## 2. The surface: `Criterion` and `Guard`

```revl sketch
service ShipTheFix {
  fn tests_pass()        -> Criterion
  fn changelog_updated() -> Criterion
}

service NoDamage {
  fn prod_touched()      -> Guard
  fn secrets_in_diff()   -> Guard
}

component Contract requires runner: TestRunner, repo: RepoState
                   provides done: ShipTheFix, safe: NoDamage {
  provide done {
    fn tests_pass()        = runner.exit_code().map((c) => c == 0)
    fn changelog_updated() = repo.diff_names().map((n) => n.contains("CHANGELOG"))
  }
  provide safe {
    fn prod_touched()      = repo.diff_names().map((n) => n.any((p) => p.starts_with("deploy/")))
    fn secrets_in_diff()   = repo.diff_text().map((t) => t.contains("BEGIN PRIVATE KEY"))
  }
}
```

### 2.1 What the two types are

`Criterion` and `Guard` are builtin type heads with no parameters. At the
value level both ERASE to `Opt[Bool]` on every tier: `Some(true)`,
`Some(false)`, `None`. A provide body implementing a `Criterion` operation
returns an `Opt[Bool]` expression, exactly as it would today; the type
annotation on the service is the only new token in the file.

The readings are fixed by the type, not by the key name:

| type | `Some(true)` | `Some(false)` | `None` |
|---|---|---|---|
| `Criterion` | this criterion is met | not met | cannot tell yet |
| `Guard` | this guard has FIRED: the run broke something | has not fired | cannot tell yet |

`None` is the in-language spelling of 441's `undecided` for one method. It is
not `unevaluable`: a verifier that is not served, a body that raises, or a
criterion in flight at a halt are runtime facts (441 §7.2, E2 and E5) and the
session records them, not the body. A body that wants to say "I cannot tell"
returns `None`; a body that cannot run at all never returns.

At the type level the two are distinct from `Opt[Bool]` and from each other in
one direction only: a `Criterion` operation's body is checked against
`Opt[Bool]` (so today's bodies type as they do), and a value of type
`Criterion` or `Guard` is usable wherever an `Opt[Bool]` is (a component may
read its own criteria; there is no reason to forbid it). What does not exist
is a way to make one: no literal, no constructor, no `type` alias resolving
to it. The only expression of type `Criterion` in the language is a call to a
`Criterion` operation. That is L4 in §3, and it is what keeps the marker
honest: the type names an obligation on the provider, and nothing an agent
writes in a turn can conjure the obligation onto an unchecked method.

**A goal service** is any service with at least one `Criterion` or `Guard`
operation. A composition's **contract** is the set of goal-service operations
it provides, folded as 441 §3.2 folds them, and its frozen identity is
`candidate_hash` over the providers of those operations. No key is named
anywhere. The two-key convention of 441 §8 is withdrawn.

### 2.2 Why a type and not a keyword

Three reasons, in order of weight.

1. **The self-host lexer oracle.** `tests/test_selfhost_lexer.py` compares
   `revl.lexer.KEYWORDS` to the set `selfhost/lexer.rvl` emits, as a set, not
   over the corpus, precisely because a corpus-only differential once missed
   `hole`. A keyword is a change to that set, a port in `lexer.rvl`, and a
   grammar-summary delta, for one bit of information. A type head is an
   identifier and the lexer never sees it. This is the argument 442 §5.1 made
   for `Delegate[S]` and 130 made for `Stream[T]`, and 441 §8 already accepted
   it ("nothing is lexed").
2. **The information is per operation, not per declaration.** A goal service
   may carry an ordinary `fn` next to a `Criterion` one (a verifier that also
   exposes a raw reading). A keyword on the `service` line would make every
   operation a criterion or force a second keyword per operation; a return
   type is already per operation.
3. **A type is what the checker already reads.** `_BUILTIN_NONRECORD`
   (`lower.py:707`) is a table of type heads with checker consequences (a
   record pattern over one is a type error). Adding two heads whose consequence
   is "the implementing body is held to L1" is the same shape of rule in the
   same table.

### 2.3 Why the service and not the component or the composition

A service is the interface every provider is upper-bounded by (G4: "a
provider body exceeds its service's declared emission bound" is a refusal).
Putting the obligation on the service means it binds ANY provider of that
service, in any composition, with no per-component annotation and no
per-composition clause to forget. A swap that replaces the contract component
with another provider of `ShipTheFix` is held to L1 without anyone re-stating
it, which is the property 441 §4.1 relies on for the amendment rule.

A composition-document clause (`goal @contract` or similar) was the runner-up.
It is rejected because it would be a second place to say the same thing, and a
place resolved header-only by a pre-linker that "is not on the trusted path"
(`src/revl/composition.py`). The row table can assert `provides done`, and
that assertion is checked against the header, so the composition document
already names the contract as a consequence of naming its rows. It needs no
clause of its own.

### 2.4 Rejected surfaces, named

* **Key-name convention (`goal`, `guard`).** 441 §8's shape. Rejected for
  §1.1's reason: the checker cannot see it, so C1 becomes a session-time rule
  and `revl check` proves nothing. It is also the 427 shape at the declaration
  level: the operator is shown a key called `goal` and the fold reads whatever
  is provided under that name.
* **`goal` / `guard` as declaration keywords.** §2.2.
* **`verified service`.** `verified` already means totality for functions and
  the inverse round-trip for effects (syntax-2.0 §7). Read-only is a third
  meaning, and a criterion is not required to be total (a `None` on a timeout
  is the honest answer, and totality would forbid the loop that waits for it).
* **A `done` or `satisfied` statement in a component body.** This is C5's
  violation spelled as syntax. 443 gave the halt no in-language surface for
  the same reason: it is the one verb the composition must never invoke on
  itself.
* **A `Contract` record type carrying state.** The state is trail vocabulary
  (`satisfied`, `violated`, `undecided`, `unevaluable`) and stays there. No
  value in the language has it, so no body can compare against it, branch on
  it, or return it.

---

## 3. What the checker proves

All six run under `revl check`, with no session and no policy, and each is a
refusal at the declaration, never a warning. L1 is the load-bearing one.

| # | Rule | Mechanism |
|---|---|---|
| L1 | **A criterion body reaches no emission and no witnessed extern.** Directly, through a plain `fn`, through an `emission` service operation off a required binding, through a spawn handle, an arrow, or a first-class reference: the six shapes the G5 walker already resolves. | The shared teardown-slot walker `_walk_inverse_emissions` (`lower.py:2658`, the one `_check_witnessed_inverse` and `_check_site_inverse_emission` both call), with a third `refuse` naming the criterion. The rule is 243 rule 3 for a witnessed inverse, applied to a new body; one walker, three diagnostics. Filed under G6 (purity outside effect forms): a criterion body is not an effect form. |
| L2 | **A goal-service operation is not an `emission`.** `emission fn x() -> Criterion` is refused at the signature, before any provider exists. | Parser, at `methodsig`. G4 already makes the service the upper bound; this keeps the bound from being declared loose. |
| L3 | **A turn may not provide a goal service.** A source admitted through item 330's crossing whose `provides` includes a service with a `Criterion` or `Guard` operation is refused at admission, naming the operation. | `Session._wire_turn`, before `_merged_turn_ir`: a header check over the turn's provides against the merged services table. 441's C2 (a criterion may not read the agent's reply) reduces to this plus G2: once every verifier a criterion requires is wired (C4), a turn cannot provide one of those keys without a G2 refusal, so the only remaining door is providing the criterion itself, and L3 closes it. |
| L4 | **`Criterion` and `Guard` have no constructor.** No literal, no `type` alias, no record field of that type, no arrow returning it. The only expression of either type is a call to a goal-service operation. | Type checker: the two heads are accepted in a service operation's return position and nowhere else in a declaration. A `let x: Criterion = ...` is refused. |
| L5 | **The operations are enumerable from the header.** Which operations are criteria and which are guards is a fact of the services table, readable without lowering a body. | The IR gains one key per operation, `"termination": "criterion"` or `"guard"`, absent otherwise (§6). `revl goal audit` and the freeze read that key. |
| L6 | **Off is byte-identical.** A program with no `Criterion` or `Guard` anywhere produces the same IR, the same diagnostics and the same emitted bytes as today. | The new key is emitted only when set; the walker runs only over bodies implementing a marked operation. Every backend golden is unchanged by the slice that lands this. |

What the checker deliberately does not prove: that a criterion is a good
proxy for the goal. 441 §5.4 states that non-guarantee and this doc adopts
it without softening. L1 is the difference between a controller and a
comment; it is not the difference between a good contract and a bad one.

---

## 4. Budgets are operator surface, not language

441 §6.4 replaced the continuation score with budgets on the axes revl counts
exactly: turns, wall clock, class-(c) crossings, consumed grant uses. It did
not say where a budget is declared. The answer follows from who the budget is
for.

A criterion is the agent's honest statement about the world, and it belongs
in source because source is checked. A budget is a bound on how much
irreversible authority a run may spend before a human looks at it. That is
not the agent's to set. A `budget { turns: 40 }` line in the agent's own
contract is the agent bounding itself, which is agent-owned termination with a
number on it: the same defect the item names, in a form that looks governed.

So a budget is declared by the operator at freeze:

* CLI: `revl run ... --goal-budget turns=40,classC=25`;
* MCP: an argument on the load or admit that freezes the contract, gated on
  the item-55 operator authority exactly as `revl_approve` is;
* trail: the declared budget and the spent counters are on every per-turn
  record (441 §9), so the operator's bound and the run's spend are readable
  together.

Two freeze-time refusals follow, both from the session and neither from the
checker, because both are about the run rather than the source:

1. **A composition that provides a goal service is loaded with no budget:
   refused.** A contract without a bound is an undecided-forever run with a
   good name. This is the pair of 441 §5.3's "an empty contract is refused".
2. **A budget is declared and the composition provides no goal service:
   refused.** Nothing to bound; the operator was about to trust a controller
   that would never engage.

Exhaustion stays what 441 §6.4 made it: a class-(c) ticket to the operator,
not a state change. Running out of turns is not `violated`.

---

## 5. Freeze, amend, settle, halt

Nothing here is new; this section says how the surface in §2 plugs into the
controller 441 already designed, so the two docs can be read as one.

* **Freeze** is admission of a composition providing a goal service, under a
  budget. The frozen identity is `candidate_hash` (`approval.py:471`) over the
  providers of every marked operation, which after 427 F4 folds the `@py`
  bodies they reach. Freezing needs no new verb (441 D).
* **Amend** is `revl_swap` under the operator authority. Every grant and
  ticket pinned to the old hash fails closed at `_find_standing_approval`
  (`session.py:3087`) and `_live_grant_for` (`session.py:3775`). Narrowing is
  not auto-ratified (441 §4.3). L3 is what makes "only through swap" true
  rather than assumed: the other path into the composition, a turn, cannot
  carry a criterion.
* **Settle** is 441 §8.1's two terms: `Session.call` (`session.py:2640`)
  refuses a class-(c) crossing while the state is `satisfied`, `violated` or
  `unevaluable` or while a budget is exhausted; `commit_confirm`
  (`session.py:1821`) refuses unless the state is `satisfied`, in the same
  drift shape it already uses.
* **Halt** dominates. `_refuse_if_halted` (`session.py:1931`) already guards
  `call`, `commit_confirm` and `abort`; a halt records the contract state as
  data and honors none of it (441 §3.1). No change to `Verdict`, no change to
  `disposition_trichotomy`.

One consequence of §2 worth stating: because the contract is the SET of marked
operations in the composition, a swap that adds a second provider of a goal
service is an amendment, and is gated and hash-invalidating like any other. A
design keyed on one `goal` key would have had to decide what a second key
means; this one does not, because the fold reads types and the hash covers
every provider that carries one.

---

## 6. The self-host port

Per the standing rule for every language feature, the port question is
answered here rather than at landing.

* **What changes in the reference.** Two type heads in `_BUILTIN_NONRECORD`;
  a `termination` key on the per-operation entry in the services IR table
  (`lower.py:6558`); the L1 walk over marked provide bodies; the L2 parser
  check; the L4 position rule.
* **What the self-host must match.** `selfhost/lower.rvl` builds the same
  services IR (`// services IR`, `lower.rvl:4632`) and the byte-agreement
  oracle compares it against the reference. The new key is emitted only when
  set, so the corpus IR is unchanged, and the port is one field on one entry
  plus the two heads in the type parser. L1's walker has its own port history
  (243 rule 3 is already in `lower.rvl`), so the criterion refusal is a third
  caller of an already-ported walk. The diagnostics must match byte for byte,
  so the message is fixed in the slice and both compilers carry it.
* **What is not ported.** L3 is a session rule and the session is not
  self-hosted. Budgets are not language. The controller is not language.

The gate: the self-host oracle for the S1 fixtures is green in the same PR, or
the new IR key is gated behind the oracle's known-divergence list with the
port as its own item. The first is the expectation; the second is the
documented fallback, not a quiet default.

---

## 7. Slice plan

Ordered so that each slice is valuable if the next never lands, and so that
nothing runtime-shaped lands before the checker rule that makes it safe to
trust. Every slice is off-by-default and byte-identical when no `Criterion`
or `Guard` is present (L6), so each can land on its own.

### S1: the type surface and the checker rules (L1, L2, L4, L5, L6)

Two type heads, the IR key, the three refusals, the self-host port.

**Oracle: `tests/test_458_criterion_surface.py`.**

* a `Criterion` body that calls a bare `emission` extern is refused, the
  message names the criterion and the extern, and the chain is rendered as the
  G5 walker renders it;
* the same through a plain `fn`, through a `witnessed` extern, and through an
  `emission` service operation off a required binding: three refusals, same
  rule;
* an `acquire`-only body and a pure body are accepted;
* `emission fn x() -> Criterion` is a parse refusal;
* `let c: Criterion = Some(true)` is refused, and a record field of type
  `Guard` is refused;
* a service with an `Opt[Bool]` operation and no marker produces an IR entry
  with no `termination` key;
* every backend golden under `tests/` is unchanged (the affected-tests gate
  runs them);
* the self-host oracle over the new fixtures agrees byte for byte.

**Exit:** the §1.1 sketch is refused by `revl check` with no session.

### S2: `revl goal audit`, the blind-spot report

441's recommendation 1, unchanged, now with a real input: the marked
operations are read from the IR key rather than from a key name. Pure
function of a compiled IR. Also lands the 441 §11.3 measurement as a script
over the item-248 harness workload.

**Oracle: `tests/test_458_goal_audit.py`.**

* a fixture composition reaching six class-(c) capabilities with a contract
  whose criteria observe two: the report names the four unobserved ones,
  each with the capability spelling `revl audit` uses;
* a composition with no goal service: the command exits with "no goal
  service" and no report, rather than an empty report;
* a criterion added that observes a third capability moves it from
  `UNOBSERVED` to `observed` and nothing else changes;
* the report is byte-identical across two compiles of the same source.

**Exit:** the measurement script produces the "observes N of M" number for
one real agent run, and the number is recorded in the roadmap item whether
it is 2 of 8 or 8 of 8.

### S3: freeze, budget and the turn rule (L3)

The two freeze-time refusals of §4, the budget argument on the CLI and MCP
surfaces, L3 in `_wire_turn`, and the hash covering the criterion providers.

**Oracle: `tests/test_458_freeze.py`.**

* load a composition with a goal service and no budget: refused, naming the
  rule; with a budget: loaded, and the trail's first record carries the
  budget;
* load a budget with no goal service: refused;
* a turn providing a goal service is refused at admission and the running
  composition is untouched (nothing plugged, nothing adopted: the same
  assertion `_wire_turn` already makes for the activation gate);
* 441 T3: mint a standing grant, swap the contract, assert exactly one fresh
  prompt on the next crossing and no error; then swap only an `@py` verifier
  body with every name identical and assert the same;
* the budget argument is refused without the item-55 operator authority.

**Exit:** the only way to change the set of marked operations in a running
composition is an operator swap, and the test proves it by trying the other
door.

### S4: the fold and the trail

Per-turn evaluation of every marked operation at the `AdmittedTurn`
boundary, the four-state max-fold, one WAL record per turn in the 441 §9
shape, `unevaluable` for a not-served verifier and for a raising body.

**Oracle: `tests/test_458_fold.py`.**

* the lattice: every pair of per-method results folds to the declared max,
  and `satisfied` requires every criterion `Some(true)` and every guard
  `Some(false)`; an empty finding list never folds to `satisfied`;
* 441 T4: make a verifier not-served mid-run; the state is `unevaluable`,
  never `undecided`;
* 441 T6: no finding carries a float and the state is one of four symbols;
* 441 T7: a composition with no marked operation writes no trail record and
  `Session.call` runs the same code path, asserted by a counter on the fold;
* a `claimed`-tier observation (441 §6.5) appears in the record and never
  moves the state.

**Exit:** the trail for a fixed workload can be re-derived from the recorded
`observed` values alone, with the agent's replies deleted.

### S5: the two chokepoint terms and the differential

The `Session.call` term and the `commit_confirm` term (441 §8.1), and the
three-run differential (441 §11.1).

**Oracle: `tests/test_458_differential.py`.**

* honest agent: `satisfied` at turn N, `commit_confirm` succeeds;
* early-claim agent: same world, replies claim done from turn 1; the
  per-turn finding vector is byte-identical to the honest run's for the same
  world state, `commit_confirm` is refused at every turn, and the state is
  never `satisfied`. **This is the exit criterion for the whole feature.**
* sabotage agent: satisfies a criterion by the degenerate route; this
  SUCCEEDS, and the test asserts that S2's report named the capability the
  route used;
* 441 T5: E-Stop a `satisfied` run; verdict `halted`, `clean` false, the
  contract state carried as data, no settlement;
* budget exhaustion raises a class-(c) ticket and leaves the state alone;
* the refusal at `Session.call` runs through the same class predicate the
  approval gate uses (442 §11's assertion, applied here).

**Exit:** replacing the agent with a liar changes nothing about the trail.

### S6: read surfaces and documentation

`revl goal report --wal FILE`, `revl_goal_state`, `revl_goal_report`, all
read-only, and the sections `tools/docgen.py --check` requires for a new
command and two new verbs (commands-reference, the guide, mcp-reference).
There is no `revl_goal_satisfy` and there never will be (C5).

**Oracle:** `tools/docgen.py --check` green; the MCP verb table renders the
two verbs with `readOnly: true`; a `revl_goal_state` call on a session with
no contract answers "no contract" rather than an empty state.

### Tier note

Criteria are evaluated by the session, which is py. A verifier placed on
another tier is reached through the placement seam like any provide method,
and a seam failure is `unevaluable`. Nothing here adds an E-Stop seam or a
latch reader to any non-py tier; 443's per-tier status is unchanged and
issue #122 stays where it is.

---

## 8. Posture: what this provides, and what it does not

**Provides.**

1. A declaration whose meaning is "this method is a termination criterion",
   checked by `revl check` with no session and no policy, so an agent cannot
   write a criterion the compiler does not recognise as one.
2. A criterion cannot cause its own satisfaction (L1), cannot be declared as
   an emission (L2), cannot be conjured by a turn (L3), and cannot be
   fabricated as a value (L4). These are refusals at the declaration, not
   runtime detections.
3. The set of criteria is a header fact (L5), so the blind-spot report and
   the freeze read the same thing the operator can read in the source.
4. The bound on a run is the operator's and is recorded next to the spend
   (§4).
5. Off is byte-identical (L6), so the feature costs nothing to a composition
   that does not use it.

**Does not provide.**

1. **A good criterion.** An agent that satisfies `file_exists("report.md")`
   with an empty file has satisfied the contract, and the controller will
   correctly say so. The blind-spot report makes the gap visible before the
   authority is spent; it does not close it. This is 441 §5.4 and it is the
   feature's honest ceiling.
2. **A statement about the world's truth.** A `Criterion` body reads what a
   verifier reports. A verifier that lies, or a test runner configured with
   zero test files, produces a `Some(true)` the checker cannot distinguish
   from a real one. What the trail records is the OBSERVED value, so the lie
   is at least auditable after the fact.
3. **Termination for a run with no contract.** A composition with no goal
   service is exactly as agent-owned as it is today. The feature is opt-in
   by construction; the measurement in S2 is what says whether the opt-in is
   being taken and how much it covers.
4. **A judgement of "worth continuing".** No score, no threshold over a
   score, no ordering among criteria beyond the lattice (441 §6, recorded so
   the question does not reopen).
5. **An E-Stop on any new tier, or a resume after one.** 443's posture is
   untouched.
6. **Enforcement outside the session.** L1, L2, L4, L5 hold under `revl
   check`. L3 and everything in §4 and §5 hold only in a session with the
   controller engaged. A composition run outside the session, with
   `revl run` and no budget, has criteria the checker has proved read-only
   and nothing that reads them.

---

## 9. Open questions, left deliberately

1. **`async fn ... -> Criterion`.** Allowed in this design (a test runner is
   asynchronous). The fold then awaits every criterion per turn, which bounds
   the turn by the slowest verifier. Whether a per-criterion timeout maps to
   `None` (undecided, the body's answer) or `unevaluable` (the runtime's) is
   decided in S4 with a test either way; the lattice makes both fail closed.
2. **Guards that fire during the criterion evaluation itself.** A guard that
   reads the same state a criterion mutates cannot exist under L1 (criteria
   do not mutate), but a guard reading state the AGENT mutates between two
   evaluations sees a torn read. S4 evaluates all marked operations in one
   pass per turn with no agent action in between, which is the answer for
   the py tier; it is stated here because a placed verifier weakens it.
3. **Whether `Guard` earns a distinct erasure.** `Opt[Bool]` with the
   `Some(true)` reading inverted is enough for the fold and keeps every
   emitter unchanged. If a later slice wants a guard to carry WHAT it saw
   (the offending path, say), that is a `Result[Unit, Str]` erasure and a
   change to the trail's `observed` field, not to the fold. Not in v1.
