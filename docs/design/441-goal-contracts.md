# 441: goal contracts, or what a gate can honestly say about "done"

Design note for roadmap item 441. Design only: no compiler change, no `src/`
change, nothing implemented. Companion docs:
[246-auto-approve.md](246-auto-approve.md),
[245-session-commit.md](245-session-commit.md),
[443-estop.md](443-estop.md),
[442-typed-delegation.md](442-typed-delegation.md),
[290-confidence-evidence-admission.md](290-confidence-evidence-admission.md),
[251-approval-distillation.md](251-approval-distillation.md),
[../gauntlet.md](../gauntlet.md).

The item asks for a goal contract: the agent drafts acceptance criteria, the
system freezes them at activation, and from then on a deterministic state
machine reading observable state (not the agent's reply) owns every
termination decision, with a per-turn trail.

**The recommendation is BUILD, but only two of the seven parts, and the
valuable one is not the state machine.** Five of the seven parts are 245/246/
344/427-F4 machinery under a new name and must not be re-implemented. Two are
genuinely new. Of those two, the state machine is the obvious one and the
cheap one; the other, a report naming the authority the contract does not
observe, is the one that answers the item's own hardest question, and it is
worth shipping even if the state machine never is.

---

## 0. The decisions, in one table

| # | Question | Decision | Section |
|---|---|---|---|
| Q1 | Who may amend a frozen contract, and does amendment invalidate prior approvals? | **Only the operator ratifies, amendment is a re-mint and not an edit, and it invalidates every approval pinned to the old hash.** This is free: an amendment is a swap, and 427 F4's `candidate_hash` already fails those tokens closed. **Narrowing is not safe to auto-ratify**, which is where a goal contract differs from a capability. | §4 |
| Q2 | The criteria are drafted by the agent. What does the operator review, and when? | **Not the criteria. The blind spot.** At freeze time the operator is shown the class-(c) capabilities in the run's reach that NO criterion observes. "Is this predicate good?" is unanswerable; "here are the six irreversible things whose done-condition says nothing about them" is answerable and header-computable. | §5 |
| Q3 | The value-of-continuation estimate | **Do not build it, and not only in v1.** A continuation score is a "probably worth it" verdict, and 290 already drew the line that revl has no "probably" verdicts. It is also not needed: revl's controller never chooses among actions, so it never needs to compare them. Bounds, not scores. | §6 |
| A | Is "done" a fourth verdict? | **No, and it is not a property of an existing one either. It is outside the verdict model, one level above it.** A verdict says what happens to a stack entry; a contract says which settling verdict is authorized and when. 443's `disposition_trichotomy` gains no column. | §3 |
| B | Is this 246's approval machinery under another name? | **Five sevenths yes.** The freezing, hash-binding, expiry, invalidation, refuse-to-effect and refuse-to-commit halves are all shipped and must be reused verbatim. Two things are new: evaluating a predicate over WORLD state, which revl has never done, and the blind-spot report. | §10 |
| C | What can the checker enforce, and what only a runtime? | The checker proves the criteria **cannot cause their own satisfaction** and **cannot read the agent's reply**. Only the runtime knows whether they hold. The split is 442's ceiling/actual split on a new axis. | §7 |
| D | New verb, new keyword? | **Neither.** A goal contract is a component providing a wiring key, freezing it is `revl_admit`, and amending it is `revl_swap`. | §8 |

---

## 1. What already exists, measured

The item claims "in revl's idiom this is not new machinery". That claim is
mostly true and it is worth measuring exactly how true, because the parts that
are already built must not be built twice, and the parts that are not must be
identified precisely rather than waved at.

### 1.1 Hash-bound consent exists, in two shapes, and both are the right shape

245 Decision 4 built the two-step: `Session.commit` (`session.py:1766`)
enumerates a manifest whose `hash` binds the gate target, and
`Session.commit_confirm(hash)` (`session.py:1777`) fires only that. If the
queue or the live composition drifted since enumeration, the recomputed hash
mismatches and the confirm is refused with a fresh manifest, "so what fires is
exactly what was approved, never a superset".

246 built the same shape for a per-call yes: a ticket bound to a hash of
exactly what it approves, expiring, dying with the candidate it named, and
non-replayable for another component.

Everything the item asks a frozen contract to be, those two already are. A
contract that could be edited in place, or whose approvals survived an edit,
would be a third and weaker consent mechanism next to two good ones.

### 1.2 The invalidation rule exists, and it was a HIGH finding

427 F4 (`9634c584`) extended `ClassMap.candidate_hash`
(`src/revl/mcp/approval.py:471`) to fold the semantic entries of the reached
externs and reached pure functions alongside the component entries, because a
swap that rewrote only an `@py` body left the hash bit-identical and carried
every standing grant across untouched. After the fix, a changed closure
recomputes a different hash and every pinned token fails closed at the existing
`continue` in `_find_standing_approval` (`session.py:3015`) and `_live_grant_for`
(`session.py:3529`): not found, one fresh prompt, no error and no silent pass.

That IS the amendment rule of §4, already implemented, for free, if a contract
is a component (§8).

### 1.3 The refusal-to-effect path exists and needs one extra input

`Session.call` is the single decision chokepoint (246 Decision 2). A class-(c)
crossing there consults the class map, and an unresolved classification is
REFUSED rather than read as class none (`session.py:3145-3163`, the 427 F1
fix), with the house phrase in the message: "an unresolved classification is
not a proof of class none (item 246, refuse-don't-degrade)".

The item's sentence "an attempt to effect past a satisfied or violated contract
fires a class-(c) ticket exactly as an unapproved crossing does today" is
therefore accurate and cheap. It is one additional term at a decision point
that already exists, not a new gate.

### 1.4 Graded knowledge exists, with the right vocabulary

The gauntlet (`docs/gauntlet.md`, `src/revl/mcp/gauntlet.py`) already separates
three kinds of knowledge about a candidate and refuses to blur them:

* **proved**: what the compiler establishes deductively before anything runs;
* **tested**: what a real boot and unload observed, with counts;
* **claimed**: what revl cannot verify and takes on faith, enumerated, because
  that list is exactly the trust surface a proof cannot cover.

A goal contract needs precisely this three-way split and should adopt the
vocabulary rather than invent a second one. §9 does.

### 1.5 The observation surface exists

`revl_live_query` answers the five query verbs against the running generation
and adds a `live` block for what only the runtime knows, including
`live.notServedNow` for a key whose provider has drifted to an inactive state.
`revl_history_emitted_between` and `revl_history_lifetime` read a recorded
timeline. `audit_diff.audit_report(ir)` (`src/revl/audit_diff.py:34`) builds the
G8 boundary surface from a compiled IR with no runtime at all.

These observe REVL's state. None of them observes the WORLD. That distinction
is §2.

### 1.6 Bounded, expiring, revocable budgets exist

294's lease and 344's standing grant carry a TTL, a uses count, a generation
binding and a revocation hook, checked at the crossing rather than at the mint
(`_live_grant_for`, `_consume_grant` at `session.py:3587`, consume-before-fire
with WAL ordering). §6 uses this instead of a continuation score, unchanged.

---

## 2. The gap, stated precisely

Six of the seven pieces the item names are in §1. What is missing is one
sentence long and it is worth stating in a form that can be checked:

> Every gate revl has reads facts about CODE (an effect class, a reach closure,
> a capability cone) or rows in a LEDGER (a grant, a ticket, a manifest hash).
> No gate in revl reads the state of the world. There is no predicate anywhere
> in the shipped tree whose truth depends on what an effect did.

Verified against `origin/main`: `goal contract`, `acceptance criteria` and
`termination controller` have zero hits outside `docs/v2.0-roadmap.md` itself.
More usefully, the structural version of the same check holds: `Session.call`'s
decision, `_enforce_sandbox`, `policy.evaluate`, `Gate.admit`,
`registry.assess_evidence` and the gauntlet's `proved` section are all total
functions of the IR plus the ledger. The gauntlet's `tested` section is the one
place revl observes an outcome, and it observes a boot of its OWN scratch
session, never a target system.

That is the whole of the gap, and it is smaller and sharper than "revl has no
goal contracts". It is also the reason this is dangerous to build casually: the
first predicate over the world is the first place revl's guarantees stop being
derivable from what it compiled.

What is NOT missing: the freeze, the consent binding, the invalidation, the
refusal to effect, the refusal to commit, the per-turn record, and the budget.

---

## 3. Question A: is "done" a fourth verdict?

**No. It is not a fourth verdict, and it is not a property of an existing one.
It is outside the verdict model and sits one level above it.**

443 made `Verdict` a three-constructor type over a single question: what
happens to an entry on the activation's LIFO stack. `commit` and `abort`
SETTLE, `halted` settles nothing, and `disposition_trichotomy` proves every
(kind, verdict) pair has exactly one of replayed / discharged / stranded, with
`book_lengths_add` proving the three lists partition the stack.

A goal contract changes none of that, and the reason is mechanical rather than
stylistic. Ask the question the verdict answers, of a contract:

> Under `satisfied`, is a `transactional` entry replayed, discharged, or
> stranded?

The question has no contract-specific answer, because a satisfied run still
commits (entries discharge) and a violated run still aborts (entries replay).
The contract never touches an entry's disposition. Adding `satisfied` to
`Verdict` would add a column to the table in which every cell duplicates the
`commit` column, which is the definition of a category error.

What the contract does is **select** a verdict and **authorize** the moment it
is taken:

| contract state | effect on settlement |
|---|---|
| `satisfied` | `commit_confirm` is authorized. Nothing commits by itself. |
| `violated` | `commit_confirm` is refused; `abort` is the only settlement. |
| `undecided` | Neither settlement is authorized. The run continues, gated exactly as today. |
| `unevaluable` | Fail closed: no settlement, and forward class-(c) crossings raise to the operator. |

So `Verdict` gains no constructor, `disposition_trichotomy` gains no case, and
the formal layer is untouched by this item. That is a decision worth stating
loudly, because the alternative is very tempting and would weaken a proof that
443 just finished making total.

### 3.1 Termination and halting are the same surface from two ends

443's insight was that `halted` exists because an operator halt "cannot
honestly claim a clean unwind", so the design names the ambiguity instead of
resolving it. The mirrored insight here:

> The E-Stop is termination with no claim. A satisfied contract is termination
> with a checked claim. The dangerous case is the one in the middle, and it is
> `undecided` forever.

An E-Stop honestly says "I am stopping and I am not telling you the world is
clean". A goal contract honestly says "I am stopping and here is the checked
reason". Agent-owned termination, which is what ships today, says "I am
stopping and here is an UNCHECKED reason", and it is the only one of the three
that lies by construction.

Two consequences follow and both should be spelled in the design rather than
discovered:

1. **`halted` dominates.** An E-Stop over a satisfied contract still halts. A
   contract state is a claim about settlement, and `halted` settles nothing, so
   a halt records the contract state rather than honoring it. `clean` stays
   `false`. There is no path by which a satisfied contract makes a halt clean.
2. **A criterion in flight at a halt is 440's ambiguous tier.** Criteria are
   read-only (§7 C1), so a halted evaluation strands nothing and invents
   nothing; its result is simply absent. The contract state at the halt is
   recorded `unevaluable` with `outcome: "unknown"`, exactly as 443 records its
   one in-flight crossing, and reconciliation is the operator's.

### 3.2 The state lattice, and why `satisfied` is the bottom

The four states are ordered, and the per-turn fold is a maximum over the
criteria, mirroring `approval.worse` (`approval.py:63`) rather than inventing a
second combining rule:

```
violated  >  unevaluable  >  undecided  >  satisfied
```

Read it as fail-closed: `violated` beats everything because a fired guard is
definite; `unevaluable` beats `satisfied` because a broken instrument is not
evidence of success; `undecided` beats `satisfied` because a criterion that
cannot yet tell is not a criterion that said yes.

**`satisfied` is the least element.** A run is done only when no criterion, no
guard and no instrument has anything to say. That is the single most important
property of the fold, and it is the reason it must be a max over a declared
order rather than an `all(...)` over booleans: an `all()` over an empty or a
partially-evaluated list is vacuously true, and a vacuously satisfied contract
is the null-contract attack in one line of Python.

---

## 4. Q1: who may amend a frozen contract

**Anyone may propose. Only the operator ratifies. Ratification is a re-mint,
never an edit, and it invalidates every approval pinned to the old contract.**

### 4.1 Amendment is a swap, so the invalidation is already implemented

If a goal contract is a component (§8), then:

* freezing it is `revl_admit` plus the plug, and its identity is
  `ClassMap.candidate_hash` over its reach closure;
* amending it is `revl_swap`, which rebuilds the class map and recomputes every
  candidate hash (`_approval_candidate_hashes`, `session.py:612`);
* every standing grant and outstanding ticket pinned to the old hash fails
  closed at the existing `continue` in `_find_standing_approval` and
  `_live_grant_for`: not found, one fresh prompt, no error and no silent pass.

So Q1's "honest default" is not a default that has to be chosen and enforced.
It is a consequence of expressing the contract in the existing composition,
and it arrives with 427 F4's fix already applied to it, including the case that
finding was about: a criterion whose `@py` body is rewritten while its name,
class and capabilities stay identical.

This is the same free win 442 found in §3.4's last row, and it comes from the
same place: bind consent to a hash of what executes, and lapse is free.

### 4.2 What is rejected

* **An agent-amendable contract.** This is agent-owned termination with extra
  steps and a longer audit trail, which is worse than agent-owned termination
  with none, because it looks governed.
* **An in-place patch operation.** 426's layer patches are the right shape for
  a composition, and the wrong shape here: a patch invites a sequence of small
  ratified widenings whose composite an operator never reviewed. 427 F3 was
  exactly a mint naming a wider cone than the operator was shown, and a patch
  stream is a machine for producing that.
* **Preserving approvals across an amendment "because it only narrowed".** See
  §4.3, which is the one place a goal contract genuinely differs from every
  other authority object in revl.
* **A `revl_goal_amend` verb.** `revl_swap` gated by the item-55 operator
  authority already is the verb, and a second one would need its own gate, its
  own drift check and its own ledger.

### 4.3 Narrowing is not safe here, and that is a real asymmetry

Everywhere else in revl, narrowing is monotone-safe. A narrower lease, a
narrower capability, a narrower distilled rule: the holder can do strictly
less, so the check can be skipped or auto-approved. 294's partial order is
built on that.

A goal contract does not have that property, because it plays two roles at
once:

* it AUTHORIZES stopping (a satisfaction criterion), and
* it BOUNDS continuing (every turn the run is not satisfied, it keeps spending
  class-(c) authority).

Tightening a criterion narrows the first role and WIDENS the second. A stricter
definition of done is a longer run, and a longer run spends more irreversible
authority. So a narrowing amendment is not auto-ratifiable, and the design must
not import the 294 intuition here.

The operator therefore ratifies every amendment in both directions, and the
review artifact is the §5 diff rather than the criterion text.

### 4.4 Amendment does not invalidate the work

The 442 D1 asymmetry applies verbatim and should be cited rather than
re-derived:

> Authority is required to incur an obligation, not to discharge one.

An amendment retires a consent row. It replays nothing, disposes no fiber,
withdraws no provision and touches no LIFO stack. Accumulated inverses stand
and settle under whatever verdict the run eventually takes. Amendment is a
forward-only, monotone predicate on authorization, evaluated at the next
crossing, exactly as 442's revocation is.

---

## 5. Q2: the contract is only as good as the drafting

This is the question the whole feature turns on, and the one where a plausible
design fails. The item states it correctly: freezing an inadequate criterion is
not safety, it is a durable mistake.

### 5.1 The wrong answer, named first

The obvious answer is "the operator reviews the criteria before the freeze".
It does not work, for two reasons that compound:

1. **Predicates do not review well.** An operator reading `fn tests_pass() =
   runner.exit_code() == 0` cannot see that the runner was configured with zero
   test files. The failure mode of a criterion is almost never in its text.
2. **The drafter is the party with an interest in weak criteria.** The agent
   writes the definition of when the agent may stop. Reviewing its wording is
   reviewing a claim by reading the claim.

A review step that does not work is worse than none, because it converts an
unreviewed contract into a ratified one.

### 5.2 The answer: review the blind spot, not the criteria

revl can compute something an operator CAN act on, cheaply, from headers alone,
and no other system in the loop can compute it:

> **The unobserved authority set**: the class-(c) capabilities in the run's
> reach closure that no criterion in the contract observes.

Both sides are already built:

* the authority side is `ClassMap`'s per-scope reach, which carries `classC`
  and `capabilities` as sets (`approval.py:332`, `:377`), aggregated the same
  way `_crossings` and `revl audit` aggregate them;
* the observation side is the contract component's own `requires` keys and the
  capability cone its criteria reach, computed by the identical walk.

The report is the set difference, rendered per capability, and it says:

```
goal contract `ship-the-fix` freezes 3 criteria over 2 verifiers.
This run's reach includes 6 class-(c) capabilities. The contract observes 2.

  observed        gwsend(host="api.internal")     tests_pass
                  fs.read(path="/repo")           no_untracked_files

  UNOBSERVED      gwsend(host="api.stripe.com")   no criterion reads this
                  term.shell                      no criterion reads this
                  fs.write(path="/etc")           no criterion reads this
                  mail.send                       no criterion reads this

The contract can be satisfied without any statement about the four unobserved
capabilities. Ratify, amend, or narrow the run's authority.
```

That is answerable. "Is `tests_pass` a good criterion" is not.

It is also the same move revl already makes one layer down, and the doc should
say so rather than present it as new: 426 §"Why this beats DSH's model" argues
that revl can show an operator "this third-party bundle widens your fs
capability to /etc" BEFORE applying it, because authority drift is computable.
The blind-spot report is authority drift on a new axis, drift between what a
run MAY do and what its own definition of done LOOKS AT.

### 5.3 When

At freeze, before the first class-(c) crossing, and again as a diff on every
amendment ratification. Not at drafting (nothing is bound yet) and not at the
end (the point is to be shown before the authority is spent).

Two admission-time refusals belong with it, because they are failures the
report should never have to render:

* **An empty contract is refused.** Zero criteria is vacuous satisfaction.
* **A criterion whose verifier is not wired is refused at admission**, not
  discovered `unevaluable` at turn forty. This is an ordinary G1 refusal on the
  contract component, and it is one of the reasons §8 makes the contract a
  component instead of a JSON blob.

### 5.4 The non-guarantee, stated rather than discovered

443's "What it explicitly does NOT guarantee" section is the right precedent
and this design needs one:

> A goal contract does not make a criterion a good proxy for the goal. An
> agent that satisfies `file_exists("report.md")` by writing an empty file has
> satisfied the contract, and the controller will correctly report `satisfied`.

What is guaranteed is narrower and still worth having:

1. the criterion was evaluated by a read-only verifier against real state, not
   inferred from the agent's reply (§7 C1, C2);
2. the criterion that was evaluated is byte-identical to the one the operator
   ratified (§4.1);
3. the operator was shown, before ratifying, exactly what the contract does
   not look at (§5.2).

Claim (3) is what makes claims (1) and (2) worth anything. A perfectly enforced
bad criterion is a durable mistake, and the report is the only artifact in the
design that makes the mistake visible while it is still cheap.

---

## 6. Q3: the value-of-continuation estimate

**Do not build it. Not in v1, and not later.** The item's instinct is right and
the reasoning should be recorded so the question does not reopen.

### 6.1 It would break a line 290 already drew

290's load-bearing decision, in its own words, is that the proposal to weigh
evidence and confidence, taken literally, "asks revl to do the one thing it
must never do: turn the gate into a heuristic", and that "there is no 'probably
safe' verdict anywhere in the system". A continuation score is a "probably
worth continuing" verdict. It is the same violation on a different axis, and
290's resolution applies unchanged: thresholds over objective recorded facts,
never a score.

### 6.2 It would recreate the 427 shape with a number as the fig leaf

427 F2/F3/F4 were one shape: the operator is shown one thing and the system
acts on another. A score is unauditable in exactly the way that matters. An
operator handed "continuing scored 0.62 against a 0.60 threshold" cannot check
the claim against anything, and the number's presence makes the decision LOOK
reviewed. That is strictly worse than agent-owned termination, which at least
does not pretend.

### 6.3 It is not needed, because revl's controller never chooses

This is the decisive argument and it is structural rather than stylistic. The
source system needs a value estimate because its termination controller chooses
among candidate ACTIONS. revl's controller does not choose actions and must
not: choosing is the agent's job, and "harness renders, revl decides" puts the
policy in the harness. revl's controller answers one question, whether a
settling verdict is authorized, and that question has no comparative structure.
Nothing is being ranked, so nothing needs a scalar.

### 6.4 What replaces it: bounds, not scores

Something must still bound a run that is `undecided` forever, and the honest
replacement is the objective kind revl already ships. A contract declares
budgets on the axes revl can count exactly:

* turns (an item-330 `AdmittedTurn` each),
* wall clock,
* class-(c) crossings (counted at the `Session.call` chokepoint today),
* consumed grant uses (`_consume_grant`, already counted).

Exhaustion is a counter reaching a declared number, not a judgment. Its outcome
is a class-(c) ticket to the operator, never a silent stop and never a state
change: **budget exhaustion is not `violated`**, because running out of turns is
not the same as having broken something, and collapsing them would make the
trail lie about which happened. Exhaustion gates forward crossings on its own
axis and leaves the contract state alone.

### 6.5 A model-graded criterion

Explicitly addressed, because it will be asked for. An LLM-as-judge criterion is
an `emission`-classed call, so C1 (§7) refuses it as a satisfaction criterion.
That is not an accident of the rule, it is the rule's purpose: a criterion that
can emit can cause its own satisfaction.

It is admissible as a `claimed`-tier observation (§9), recorded in the trail
next to the proved and tested ones and visibly weaker than both. A `claimed`
observation may never move the contract state. That is the gauntlet's existing
discipline applied unchanged, and it lets the harness render a rubric score
without letting the score decide anything.

---

## 7. Question C: what the checker enforces, what only a runtime knows

The split is 442 §4.3's, on a new axis: **the checker proves the criteria
cannot cheat; the trail records what they found.**

### 7.1 Proved statically, at admission

| # | Property | Mechanism |
|---|---|---|
| C1 | **A criterion cannot cause its own satisfaction.** The contract component's reach closure contains no emission and no witnessed mutation: every one of its scopes classifies to class `None`. | `ClassMap.classify_call` over the contract's provide-methods, refusing an UNRESOLVED classification exactly as `session.py:3145` does. Class none is pure and acquire only, which is precisely "reads, never writes". |
| C2 | **A criterion cannot read the agent's reply.** The contract component's `requires` set is disjoint from the keys the turn provides, so no binding in a criterion's scope resolves to the agent's output. | An ordinary G1 name check plus a set difference over the item-330 turn's provided keys. |
| C3 | **The contract is non-empty and its criteria are enumerable.** Zero criteria is refused; the criterion set is the provided service's method list, fixed at admission. | The service declaration. Header-only, so §5.2 needs no lowering. |
| C4 | **Every verifier a criterion names is wired.** A criterion over an unwired service is refused at admission, not `unevaluable` at turn forty. | G1, unchanged. |
| C5 | **No in-language surface can set the contract state.** No extern, no stdlib binding, nothing an `.rvl` body can reach. | 443's precedent verbatim: the E-Stop has no in-language surface for the same reason, that it is the one verb a composition must never invoke on itself. |
| C6 | **The frozen identity covers what executes, not what is named.** The contract's hash folds the semantic entries of its components AND the externs and pure functions they reach. | `candidate_hash`, post-427-F4, unchanged. |
| C7 | **The blind-spot report is computable before anything runs.** | `classC` and `capabilities` sets on the reach, minus the contract's own cone. |

C1 is the one that matters. Everything else is hygiene; C1 is the difference
between a termination controller and a very long comment.

### 7.2 Knowable only at runtime

| # | Property | Mechanism |
|---|---|---|
| E1 | Whether a criterion holds right now. | The criterion call, per turn. |
| E2 | Whether its verifier is actually serving. | `revl_live_query`'s `live.notServedNow`. A drifted verifier is `unevaluable`, never `undecided`. |
| E3 | Budget counters. | The `Session.call` chokepoint and `_consume_grant`, both already counting. |
| E4 | Which turn the state changed on, and on which criterion. | The trail, §9. |
| E5 | Whether a criterion was in flight at a halt. | 443's `_InFlight` seam, 440's `unknown` tier. |

The line between the tables is the line between "cannot cheat" and "did not
cheat". The checker gives the first. Nothing gives the second, and §5.4 says so.

---

## 8. Question D: the surface, and why there is no new verb

**A goal contract is a component that provides a wiring key. Freezing it is
admission. Amending it is a swap. There is no new keyword and no new verb.**

```revl sketch
service ShipTheFix {
  fn tests_pass()        -> Opt[Bool]
  fn changelog_updated() -> Opt[Bool]
}

component Contract requires runner: TestRunner, repo: RepoState
                   provides goal: ShipTheFix {
  provide goal {
    fn tests_pass()        = runner.exit_code().map(fn(c) = c == 0)
    fn changelog_updated() = repo.diff_names().map(fn(n) = n.contains("CHANGELOG"))
  }
}
```

Every design property falls out of that shape rather than being enforced
separately:

* **The criteria are enumerable at admission**: they are the methods of a
  declared service (C3).
* **The verifiers are declared services**: `requires runner`, `requires repo`
  (C4), which is the item's own wording, satisfied by ordinary wiring.
* **The read-only rule is one predicate over shipped code**: the provider of
  `goal` classifies to class `None` on every method (C1).
* **The freeze is `revl_admit`** and the frozen identity is `candidate_hash`
  (C6).
* **The amendment rule is `revl_swap`** and its invalidation is 427 F4's
  (§4.1).
* **Nothing new is lexed.** `Opt[T]` is a type constructor and the rest is
  ordinary component syntax, so the self-hosted lexer's keyword-set parity
  oracle is untouched. This is the argument 442 §5.1 made for `Delegate[S]`,
  243 made for `witnessed` and 308 made for `shared`.

Guards, the violation-relevant half, are a second key on the same shape
(`provides guard: ...`), so the fold in §3.2 reads two keys and needs no
naming convention and no annotation. A guard returning `Some(true)` means the
run has broken something; a criterion returning `Some(true)` means the run has
achieved something. Two keys, one lattice.

### 8.1 The controller sits at two shipped chokepoints and adds none

| where | shipped today | what 441 adds |
|---|---|---|
| `Session.call` (`session.py:2568`) | the 246 per-call class decision, with the 427 F1 refuse-don't-degrade branch | one term: a class-(c) crossing attempted while the state is `satisfied`, `violated` or `unevaluable`, or while a budget is exhausted, raises a ticket instead of firing |
| `Session.commit_confirm` (`session.py:1777`) | the hash-bound two-step, refusing on drift with a fresh manifest | one term in the gate target: the confirm refuses unless the state is `satisfied`, refused with the same drift shape and a fresh manifest |
| `AdmittedTurn` (item 330) | the per-turn admitted composition whose crossings register into the enclosing 245 frame | the trail's key |

That is the whole runtime surface. No new chokepoint, no new ledger, no new
decision point that could disagree with an existing one. The 442 §11 exit
assertion applies here too and should be a test: assert the contract's refusal
runs through the same predicate the approval gate's does, so the two can never
disagree about whether a crossing is class (c).

### 8.2 Read surfaces

| surface | verb | note |
|---|---|---|
| CLI | `revl goal audit` | the §5.2 blind-spot report, from a compiled IR, no runtime |
| CLI | `revl goal report --wal FILE` | the trail, read back durably |
| MCP | `revl_goal_state` | read-only: the state, the per-criterion findings, the budgets |
| MCP | `revl_goal_report` | read-only: the trail |
| MCP | ratification | `revl_swap` under the item-55 operator authority, unchanged |

All four are read-only. There is deliberately no `revl_goal_satisfy`, for C5's
reason.

---

## 9. The trail

One record per turn, appended to the WAL 245/246 already write, with the
gauntlet's tiers on each finding:

```
{ "turn": 12, "contractHash": "sha256:…", "state": "undecided",
  "findings": [
    {"criterion": "tests_pass", "tier": "tested",
     "verifier": "runner", "observed": {"exitCode": 1}, "value": false},
    {"criterion": "changelog_updated", "tier": "tested",
     "verifier": "repo", "observed": {"names": ["src/x.rvl"]}, "value": false}
  ],
  "guards": [],
  "budgets": {"turns": {"spent": 12, "of": 40},
              "classC": {"spent": 3, "of": 25}},
  "unobservedAuthority": 4 }
```

Three properties the shape is chosen for:

1. **`tier` is per finding, never per contract.** A `claimed` finding is
   visibly weaker in the same record as a `tested` one, and §6.5's rule
   ("`claimed` may not move the state") is checkable by reading one field.
2. **`observed` carries the value the criterion read, not the criterion's
   verdict alone.** A trail that records only `false` cannot be re-derived; one
   that records the observation can, which is what makes the exit test in §11
   possible at all.
3. **`unobservedAuthority` is on every turn**, not only at the freeze, because
   a swap mid-run can widen the actor's cone under an unchanged contract. That
   is the 427 shape (shown once, enforced against something later) and carrying
   the count per turn is what closes it.

`state`, `unevaluable` findings and in-flight criteria reuse 247's `unresolved`
audit state and 440's `unknown` outcome rather than adding vocabulary.

---

## 10. Question B: is this 246 under another name?

442's design pass concluded that delegation substantially collapsed into 294's
leases, and recommended rescoping. The same question, asked honestly here, gets
a split answer rather than a yes or a no.

| part of the item | new? | what it already is |
|---|---|---|
| Freeze the contract at activation | **no** | `revl_admit` plus `candidate_hash` |
| Bind consent to a hash of exactly what it approves | **no** | 245 Decision 4, 246's ticket |
| Amendment invalidates prior approvals | **no** | 427 F4, `_find_standing_approval` failing closed |
| Effecting past a decided contract fires a class-(c) ticket | **no** | one term at the `Session.call` chokepoint |
| Refuse to settle when the contract does not hold | **no** | one term in `commit_confirm`'s gate target |
| An auditable per-turn trail | **no** | the 245/246 WAL, plus item 330's turn key |
| A budget that bounds a non-terminating run | **no** | 294 TTL/uses, `_consume_grant` |
| **A predicate over WORLD state deciding anything** | **YES** | nothing. §2 |
| **The unobserved-authority report** | **YES** | nothing, though 426's authority-drift rendering is the same move one layer down |

Seven of nine parts are shipped machinery. The item's claim that "in revl's
idiom this is not new machinery" is therefore correct about the plumbing and
incorrect about the substance, and the two new rows are exactly where the
design effort belongs.

The finding worth recording against item 246, in the way 442 recorded one:
**246's three postures answer "may this crossing happen" and were never
extensible to "should this run stop", because every input 246 reads is a fact
about code or a row in a ledger.** A goal contract is not a fourth posture. It
is the first gate input that is not derivable from what revl compiled, and it
should be built with that stated on its face.

---

## 11. The exit test: what proves a termination controller WORKS

A controller that merely EXISTS passes every obvious test: it has states, it
transitions, it writes a trail, and a demo run reaches `satisfied`. None of
that distinguishes it from a controller that reads the agent's reply through
three layers of indirection.

The threat model is one adversary with two strategies. The agent wants to stop
early (it is expensive to keep working) or to run long (it is not finished and
will not say so). So:

> **The controller works if and only if replacing the agent with a liar
> changes nothing about the trail.**

Everything else is plumbing. The battery below is the mechanical form of that
sentence, in the style of 438's falsification harness: it lands as one test
file whose assertions are the design's claims, so the recommendation can be
re-checked by running it rather than by rereading the argument.

### 11.1 The three-run differential, which is the actual exit criterion

One fixed workload, one frozen contract, three agents.

1. **Honest.** Does the work, reports done when done. The controller reaches
   `satisfied` at turn N, and `commit_confirm` succeeds.
2. **Early-claim.** Same workload and same world, but the agent's reply claims
   completion at turn 1 and at every turn after, and it does none of the work.
   **Assertion: the per-turn finding vector is byte-identical to run 1's for
   the same world state, `commit_confirm` is REFUSED at every turn, and the
   state is never `satisfied`.** This is the falsifiable form of "reads
   observable state rather than the agent's own reply", and it fails loudly for
   any implementation in which the reply reaches the controller through any
   path at all.
3. **Sabotage.** The agent satisfies the criteria by the degenerate route: it
   writes the empty file the criterion checks for. **Assertion: this SUCCEEDS**,
   and the test asserts it succeeds. What must also hold is that the freeze-time
   blind-spot report NAMED the capability the degenerate route used. A design
   whose test suite pretends this case fails is a design that will be trusted
   past its guarantee, so this assertion exists to keep §5.4 honest.

Run 2 is the exit criterion. Runs 1 and 3 exist to keep it interpretable.

### 11.2 Seven structural assertions an existing-but-broken controller fails

* **T1, no self-satisfaction.** A candidate whose `goal` provider reaches an
  emission is refused at admission, naming C1. Reproducer: a criterion that
  writes the file it then checks for. A controller without T1 has an agent
  whose termination condition it can dictate.
* **T2, no reply channel.** A contract whose `requires` set intersects the
  turn's provided keys is refused, naming C2. This is the static half of run 2.
* **T3, the freeze is real.** Mint a standing grant, amend the contract, assert
  exactly one fresh prompt on the next crossing and no error. Then amend by
  rewriting only an `@py` verifier body, keeping every name, class and
  capability identical, and assert the same. The second half is 427 F4's
  reproducer pointed at the contract.
* **T4, fail-closed instrument.** Make a verifier not-served mid-run. Assert
  the state is `unevaluable` and NOT `undecided`, that `commit_confirm`
  refuses, and that the next class-(c) crossing raises a ticket. This is the
  427 F1 shape, which was a CRITICAL, restated for this feature: an
  unevaluable criterion is not a proof that the run may continue.
* **T5, `halted` dominates.** E-Stop a run whose contract is `satisfied`.
  Assert the verdict is `halted`, `clean` is `false`, the halt record carries
  the contract state as data, and no settlement occurs.
* **T6, no scoring.** The state is one of exactly four symbols, no finding
  carries a float, and no ordering exists over criteria beyond the §3.2
  lattice. Crude, and it is the assertion that keeps §6's answer from eroding
  one convenience at a time.
* **T7, off is byte-identical.** A composition with no `goal` key behaves
  bit-for-bit as it does today, with no trail records written and no extra
  work at the `Session.call` chokepoint. This is the house standard: 246 shipped
  "policy-off byte-identical", 443 shipped "byte-identical to the pre-443
  runtime".

### 11.3 The measurement, so "works" has a number

One number, taken on the item-248 harness workload the 245/246 arc is already
measured on:

> Of the class-(c) capabilities a real agent run reaches, what fraction does
> its own drafted contract observe?

If a typical agent-drafted contract observes two of eight, that is the feature's
finding and it is worth publishing whether or not the state machine ships,
because it quantifies how much of an agent run's irreversible authority sits
outside its own definition of done. If it observes eight of eight, the feature
is less urgent than the item claims and the roadmap should say so. Either
answer is a result; neither is available today.

---

## 12. Recommendation

1. **Build the blind-spot report first, and separately (§5.2).** It is a pure
   function of a compiled IR, it needs no runtime, no state machine, no new
   consent path and no verdict change, and it is valuable even while
   termination stays agent-owned: it tells an operator what a run's own
   definition of done ignores. It is also the only part that answers Q2, and Q2
   is the question the feature's value turns on. Ship it as `revl goal audit`
   plus the §11.3 measurement.
2. **Then build the controller as one term at each of two shipped chokepoints
   (§8.1),** with the four-state lattice (§3.2) and the trail (§9). Reuse
   `candidate_hash`, `_find_standing_approval`, `commit_confirm`'s drift shape
   and the gauntlet's tier vocabulary verbatim. Nothing new is minted, expired,
   revoked or hashed.
3. **Do not extend `Verdict` (§3).** `disposition_trichotomy` and
   `book_lengths_add` stay total with three constructors. A contract selects a
   verdict; it is not one.
4. **Do not build a continuation score, ever (§6).** Record the reason in the
   roadmap item so the question does not reopen: revl's controller never
   chooses among actions, so it never needs to compare them, and a score would
   put a heuristic where 290 established there are none.
5. **Rescope item 441** from "a goal contract subsystem" to "one report, one
   four-state fold at two existing chokepoints, and one checker rule (C1)". The
   item is right about the gap and overstates the machinery, in the same way and
   for the same reason 442 did.
6. **Record the finding against item 246.** Its three postures answer "may this
   crossing happen" and are not extensible to "should this run stop", because
   every input 246 reads is a fact about code or a row in a ledger. A goal
   contract is revl's first gate input that is not derivable from what it
   compiled, and that should be stated on the feature's face rather than
   discovered by whoever trusts it too far.
