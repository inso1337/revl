# Design: UI transactions and honest non-invertibility

Design-doc id 536. 531 to 535 are taken on open branches, so this is the next
free number checked against every remote ref rather than against `main` alone
(four lanes collided on 531 by checking `main`); the roadmap item of the same
number is unrelated, which is the convention 457 established. Roadmap item
served: 522 (issue #1196), from the 2026-09-19 external review. Direct
dependency: 521 (issue #1195, PR #1228), whose reserved computer-use namespace
this extends and whose design doc is `docs/design/532-typed-computer-use.md`.
Adjacent and deliberately not restated here: 546 (issue #1225, no inverse for
accumulated state, section 7 below), 243 (the witness/inverse pair), 254 (the
compensate-grade crossing on the erase report), 246 (the approval authority),
249 (taint and provenance), 539 (the computer-use substrate, upstream
`inso1337/revl-harness#11`).

Sources studied, all at `b8fea480` on `agent/1195-typed-computer-use`:
`src/revl/ui_family.py`, `src/revl/parser.py` (`extern_decl`,
`_capability_list`, `_capability_params`, `_emit_step`), `src/revl/lower.py`
(`_approval_index`, `_approval_covers`, `_emit_crossed_caps`,
`_lower_emit_step`), `src/revl/diagnostics.py` (`GUARANTEES`, `classify`),
`src/revl/erase_report.py`, `src/revl/cap_order.py`,
`tests/fixtures/erase_net.rvl`, `tests/test_erase_report_network_254.py`,
`tests/test_502_approval_replay.py`, `tests/test_approval_policy.py`,
`tests/test_ui_capability_namespace_521.py`, and
`docs/design/532-typed-computer-use.md`.

## 0. The decision in one paragraph

A UI transaction is not a new effect construct. It is a **classification plus
a set of refusals over the classification**, because the only thing revl can
honestly own about a target whose state it does not hold is whether a claim
about that state is admissible. Every computer-use verb carries one of five
reversibility classes, the class is **registry-owned** rather than
author-declared, and `unknown` is handled exactly as `irreversible`. Slice 1
lands the classification and the two refusals that keep the residue report
truthful: a verb with no inverse may not declare one, and a verb that has one
must. The phase list the item proposes (observe, plan, stage, preview,
confirm, commit, verify, compensate) is a runtime shape; what revl decides is
which phases a given step is even eligible for, and that is a compile-time
fact read off the class. The confirmation gate is specified here and is
deliberately NOT in slice 1, for a measured reason given in section 6.

## 1. The problem in the guarantee's own terms

G4 says every mutation carries an inverse, or admits irreversibility with
`emit`. G7 says teardown is derived and LIFO. Both are about effects revl
records. Item 521 put a computer-use verb on the boundary surface, so revl now
enumerates what a UI agent can reach. It says nothing about what happens when
the third of five steps fails.

Three things go wrong at once, and they are not the same thing.

**The sequence has no unit.** Five `emit`s in a method are five crossings.
There is no object that owns them jointly, so there is nothing for a failure
to be a failure OF.

**The residue proof has nothing to measure.** `erase_report` reads whether a
crossing is compensated, and for a host emission extern it reads the
extern-declared `compensate` slot (item 254 fixed exactly that: before it, a
compensate-bearing emission extern was misreported bare). So the residue
report will happily read a `compensate` an author wrote on a `ui.click`, and
print a clean teardown for a step whose effect the application never
published an inverse for. That is not a missing feature, it is a FALSE
MEASUREMENT, and it is the fail-open shape: a cleanliness claim outliving the
thing that was meant to bound it.

**"Rolled back" covers two outcomes.** Issue #1225 makes the same argument for
accumulated state and asks that a rollback report which layers it restored and
which it could only compensate. A UI transaction's residue is that shape
exactly.

Slice 1 attacks the second one, because it is the one that makes the other two
undiagnosable. A transaction built on a residue report that can lie is worse
than no transaction.

## 2. The five classes, and what the system does with each

The classification is in `src/revl/ui_family.py` beside the verb registry, and
it is CLOSED and TOTAL over the admissible spellings: a verb with no class
would default to "not checked", which is the fail-open default this item
exists to remove. `tests/test_ui_transaction_classification_522.py` asserts
exact equality between the class table's keys and `ui_family.spellings()`, so
a verb added without a class fires there rather than shipping unclassified.

| class | what it means | what the system does |
| --- | --- | --- |
| `reversible` | the step changes no state the target owns | nothing is required and nothing is registered; a transaction skips it on the way back |
| `compensatable` | an inverse exists, but only the author can write it | the declaration MUST carry `compensate`; the transaction registers it and runs it LIFO |
| `confirm-required` | the step may proceed only behind an explicit human confirmation | refuse to execute without one. No verb is BORN in this class (section 6) |
| `irreversible` | no inverse exists | the transaction reports `uncompensated`, never `no_residue`; a declared `compensate` is REFUSED rather than believed |
| `unknown` | revl cannot tell | treated exactly as `irreversible`, which is the direction that is safe when the answer is missing |

The assignment, and the reason for each:

| token | class | why |
| --- | --- | --- |
| `screen.observe` | `reversible` | a read. Nothing in the target changes, so there is nothing to undo |
| `ui.find` | `reversible` | derives a target from observed content; it actuates nothing |
| `ui.text` | `compensatable` | key input into a named field. The inverse is restoring that field, which the author knows and revl does not |
| `ui.click` | `unknown` | actuates a control whose effect the application never published. Not "probably fine": revl has no answer at all |
| `ui.download` | `irreversible` | a file landed on the host. Deleting it does not un-fetch it, and the fetch may already have been metered or logged on the other side |

### 2.1 Why the class is registry-owned

An author may not declare that a click is reversible. This is item 249's
argument moved one surface over: sink-ness is derived from the side that
GRANTS authority, never from an author's qualifier, because a classification
an author can lower is a classification a careless author lowers, and the
hostile case is not even the interesting one. The same reasoning rules out
carrying the class as an item-294 capability parameter
(`ui.click(effect="reversible")`), which is why the lookup strips a valuation
before reading the table: a narrowing must not be able to move a token to a
weaker class.

The cost is real and is accepted: revl cannot know that in THIS application
the Cancel button is reversible. The answer to that is not an author
assertion, it is a narrower verb in the closed registry, which is a change to
`ui_family` with a stated emission class and a stated failure direction.

### 2.2 `unknown` is not a fifth "maybe"

`unknown` and `irreversible` are separated for the diagnostic and joined for
the decision. They are separated because the refusal text differs and an
author reading "revl cannot tell" is being told something true that
"irreversible" would overstate. They are joined because a transaction that
treats an unclassified step as reversible is precisely the fail-open shape,
and `ui_family.NO_INVERSE` is the single place that join is written down.

## 3. The phases, and which of them revl decides

The item proposes observe, plan, stage, preview, confirm, commit, verify,
compensate. Writing that list down is not the design. The design is which of
those phases revl decides and which the substrate runs, because an implied
guarantee revl does not enforce is worse than no phase list at all (item 539's
own argument, and 532 section 4.3 restates it for the ladder).

| phase | who owns it | what revl decides |
| --- | --- | --- |
| observe | substrate | that the result is attacker-influenced content and never a trusted reference (532 section 5) |
| plan | substrate | nothing. revl bounds reach, not order |
| stage | substrate | nothing in slice 1 |
| preview | substrate | nothing. A preview is a rendering |
| confirm | revl and the operator | that a `confirm-required` step does not execute without one (section 6) |
| commit | substrate | which verbs may be crossed at all, by the existing G4 subset and G8 reach |
| verify | substrate | nothing in slice 1. The postcondition is a read, and reads are the substrate's |
| compensate | revl | that the registered compensations MATCH the classification: present where one exists, absent where none does |

The one phase revl fully owns is `compensate`, and it owns it statically. That
is not a reduction of the item, it is where the item's leverage actually is:
every other phase's honesty depends on the compensation set being true, and
the compensation set is a declaration.

### 3.1 Why `verify` reads a postcondition and not a return code

Kept from the item because the transaction is unsound without it. A UI
actuation's return value says the actuation was delivered. It does not say the
business effect occurred: a click can land on a control that was re-rendered,
on a disabled button that swallows it, or on the right button in an
application that then failed server-side and rendered an error the agent did
not read. So `verify` is a fresh `screen.observe` plus a `ui.find` against an
expected postcondition, which is why both of those verbs are `reversible` and
may appear inside the verify phase without adding to what the transaction has
to undo.

## 4. Honest non-invertibility: the four hazards

Stated one by one, with what slice 1 does and does not do about each.

**A click may trigger something no inverse describes.** This is the `unknown`
class. Slice 1 refuses the false claim. It does not prevent the click.

**UI state can change between observation and execution.** This is a
check-to-use race, structurally identical to the finding in item 510 where a
tier resolved a pinned asset's leaf twice and a swap between the guard and the
use was followed by name. The fix pattern there was: resolve once, act on the
handle, never by name. Applied here, a UI target must be resolved once into a
binding that carries its evidence, and every later phase must act on that
binding rather than re-resolving the name. That is item 521's slice 4
(`UiTarget` as a record carrying the window identity, the accessibility
identity, the evidence hash, the allowed action, an expiry), and this item
DEPENDS on it: a transaction over targets re-resolved by name has a race in
every phase boundary. Slice 1 does not close it, and section 11 says so.

**Accessibility metadata can be incomplete or spoofed.** A target's declared
role and name come from the application, and a hostile or merely broken
application is the same input to revl. 532 section 5 already says the target
is `Untrusted[UiTarget]` for this reason. The transaction consequence is that
the `verify` postcondition cannot be believed more than the `observe` it was
read from, so a verified postcondition raises confidence and does not
establish a fact. A transaction that reported `verified` on the strength of a
spoofable read would be inventing a guarantee.

**A successful click does not prove the business effect.** Section 3.1.

## 5. The refusals slice 1 lands

Both at the extern declaration, both `code="G4"`, `category="reversibility"`.
G4 is `every mutation carries an inverse, or admits irreversibility with
emit`, so a UI step declaring an inverse it does not have is that guarantee's
own failure and carries its code rather than a new one.

**R1: a verb with no inverse may not declare one.**

```revl reject G4
extern emission[ui.click] fn click(target: Str) compensate undo_click()
  = @py { pass }
```

The declared `compensate` is not decoration. It lives on the extern IR entry
and is what the erase and residue reports read to mark the crossing
compensated. Admitting it lets a transaction over a step with no inverse print
`no_residue` where the item's exit says it must report `uncompensated`.

**R2: a compensatable verb must declare one.**

```revl reject G4
extern emission[ui.text] fn type_into(target: Str, s: Str) = @py { pass }
```

`ui.text` has an inverse and only the author can write it. A missing
compensation is residue the transaction could have avoided and will not even
be able to name, which is the worst of the three outcomes: not restored, not
reported.

The admitted forms:

```revl fragment
extern emission[ui.text] fn type_into(target: Str, s: Str)
  compensate restore_field()
  = @py { pass }

extern emission[ui.click] fn click(target: Str) -> Int = @py { return 0 }
```

### 5.1 Why the declaration and not the emit

The obligation is on the thing that crosses. An emit-site `compensate` is
per-call and an author can simply not write it at one of five call sites; an
extern-declared one holds for every crossing of that extern by construction,
and it is the slot item 254 already wired into the residue report. A service
method's `emission[...]` scope declares an interface and not a crossing, and
is deliberately untouched, so `service S { emission[ui.text] fn go(...) }`
still parses. The G4 subset rule then makes the provider's reach land on an
extern that did carry the obligation.

## 6. The confirmation gate, and why it is not in slice 1

The item is explicit that `preview` and `confirm` must reuse the existing
approval authority rather than inventing a second one, and this design agrees.
`confirm-required` is therefore defined as a class a token is RAISED to by
that authority, not a class a verb is born in, which is why no row in section
2's second table carries it. The test file asserts that emptiness on purpose,
so a later slice that assigns the class without landing the raise fires a test
rather than shipping a label nothing acts on.

It is not in slice 1 because of a measured limit. `Approval[C]` is
activation-scoped and non-storable by construction: `await approval[C] { ... }`
is allowed only in a component activation body, and the value cannot be
written as a type, stored, returned, or passed as a parameter (item 246's
non-persistence rule, which the refusal text states directly). A computer-use
loop lives in a `provide` method: it observes, finds a target, and actuates
it, and the target is a value computed in that method. So the only
unforgeable confirmation revl has today cannot reach the place a UI
transaction needs it, and a slice that mandated it would not gate the feature,
it would refuse the feature.

Writing that down is the point. The alternatives, with their costs, so slice 2
starts from a decision and not from a blank page:

- **Extend `Approval[C]` to a provide method.** Smallest surface, but item
  246's non-persistence rule is load-bearing (an approval that crosses a
  snapshot, handoff or spawn boundary is a replayable token, which item 502
  exists to prevent). This is a change to that rule and must be argued there,
  not here.
- **A per-step confirmation ticket, minted and consumed inside one
  transaction.** Matches the phase list and scopes the token to the
  transaction rather than to the activation, but it is a second authority
  unless it is spelled as a restriction of the first.
- **Operator-side only, via `capability ui.* requires approval`.** Already
  works as a selector over a declared token (521 measured it), costs nothing,
  and is not sufficient on its own, because a policy rule is optional and
  absence is silence.

Until one of those lands, the honest statement is the one this design makes:
slice 1 stops a program from CLAIMING cleanliness it cannot have, and does not
stop it from clicking. Section 11 repeats that so a reader does not infer
more.

## 7. Relationship to issue #1225 (item 546)

#1225 argues that revertible effects give no inverse for ACCUMULATED state: a
routing table that drifted over two hundred generations, a trust score, a
retention index. Its exit asks that every evolvable layer declare whether its
state is revertible, compensatable or neither, that a layer which is neither
may not be promoted without a declared and tested compensation path, and that
a rollback report which layers it restored and which it only compensated.

**This design ANSWERS its shape for one layer and DEFERS its substance.**

Answered: the external UI is a layer whose state revl does not own, its
classification is declared, the class is registry-owned rather than a promise,
and the refusal in section 5 is precisely #1225's "may not be promoted without
a declared compensation path" applied per verb. The failure direction is the
same one: "rolled back" stops being one word covering two outcomes, because a
step with no inverse cannot be spelled as one with an inverse.

Deferred, and not answered: a UI transaction's residue INSIDE the target
application is accumulated state in exactly #1225's sense. Compensating a
`ui.text` restores a field; it does not remove the autosave revision the
application wrote, the entry in its own undo stack, the row in its audit log,
or the increment to a rate limiter. Per-step compensation is bounded by what
one step's inverse touches, and accumulated state is by definition what
survives that. This design does not claim otherwise, and a transaction built
on it must report `uncompensated` for the accumulation even when every
registered compensation ran. The general answer belongs to #1225; naming the
overlap here is so that a later slice of either item does not quietly assume
the other solved it.

Two vocabularies are in play and they should be reconciled rather than left to
drift: #1225 uses three classes (revertible, compensatable, neither), this
item uses five. `neither` splits into `irreversible` and `unknown`, and the
split is worth keeping because the diagnostics differ; `confirm-required` is
orthogonal to both and could apply to an evolvable layer too. If #1225 lands
first, this table should be read as a refinement of its three, not as a rival.

## 8. Failure direction of every decision here

| decision | fails | why that is the safe side |
| --- | --- | --- |
| the class is registry-owned | closed | a classification an author can lower is one a careless author lowers (item 249, Slice D) |
| an item-294 valuation cannot change the class | closed | otherwise a narrowing would be a weakening, which inverts what narrowing means |
| `unknown` is treated as `irreversible` | closed | the missing answer is the case the whole item exists for |
| a verb with no inverse may not declare one | closed | the alternative is a residue report that prints `no_residue` for a step that left residue |
| a compensatable verb must declare one | closed | a compensatable step with nothing registered is avoidable residue that the transaction cannot even name |
| the check is at the extern declaration, not the emit | closed | per-crossing, the author can omit it at one call site of five; per-declaration it holds for every crossing |
| a service method's scope is not checked | open, deliberately | a service method declares an interface, not a crossing; refusing there would make the interface carry an obligation it cannot discharge, and G4's subset rule already routes the reach onto an extern that can |
| the confirmation gate is absent rather than approximated | neither; it is a scope statement | an approval that cannot reach the crossing would be a word the author writes and nothing reads, which is the shape this repository has already measured ten times |

## 9. Self-host

Does this need a self-host port? **Not in slice 1**, and for the same
structural reason 532 section 9 gives: `selfhost/lower.rvl`'s `admit_src`
issues no admission at all (its verdicts are `Refused`, `NoObjection` and
`OutsideFrontier`, `"admitted": false` on every arm, item 417), so a program
carrying a computer-use token the self-host does not understand cannot be
admitted by it. The gate is already fail-closed for this construct.

The obligation is recorded so a later slice does not rediscover it: **when a
slice of this item starts deciding admission on a reversibility class,
`selfhost/lower.rvl` must either port the check or decline the program by a
NAMED marker**, never by agreeing with silence. The oracles catch divergence,
not missing features, so a check the self-host simply does not run leaves
every oracle green (item 391's finding, restated by 417).

No `selfhost/*.rvl` file is touched by slice 1, and
`tools/build_gate_crate.py --check` reports in-sync, so no crate is
regenerated.

## 10. Slice plan

Each slice is closable on its own and carries the oracle that makes it a
measurement. Slice 1 is landed; the rest are proposals.

**Slice 1: the classification and the two teardown refusals. LANDED.**
`src/revl/ui_family.py` gains the five classes, the per-verb table, the
`NO_INVERSE` join and `teardown_refusal`; `parser.extern_decl` gains one hook
where the capability scope, the extern name and the `compensate` clause are
all in hand. Oracle: `tests/test_ui_transaction_classification_522.py`, 15
tests. Non-vacuity measured against `agent/1195-typed-computer-use` at
`b8fea480`: the five refused programs are all ADMITTED there and refused here,
and the six controls are admitted on both trees.

**Slice 2: the confirmation gate.** Decide between the three options in
section 6 and land the raise to `confirm-required`, so an `unknown` or
`irreversible` crossing without a confirmation is refused by name. Oracle: the
same program admitted with a confirmation and refused without it, plus a
control on an ordinary capability. This is the slice that turns section 6's
honest gap into a check, and it is the largest remaining piece of the item.

**Slice 3: the transaction unit and LIFO compensation.** A declared sequence
whose registered compensations run in reverse on an unmet postcondition. Needs
slice 2 for `confirm`, and needs item 521's slice 4 for a target that survives
a phase boundary. Oracle: a five-step transaction whose third step fails runs
exactly the compensations of steps two and one, in that order, and reports the
third as uncompensated.

**Slice 4: `uncompensated` on the residue report.** Extend the residue and
erase surfaces so a UI transaction's outcome is a third value beside
`no_residue` and a bare crossing, and so a transaction that ran every
registered compensation over a step set containing an `irreversible` verb
still reports `uncompensated`. Oracle: the report for the transaction in slice
3 names both the compensated steps and the uncompensated one, and does not
print `no_residue`.

**Slice 5: the postcondition.** A typed postcondition per step, read by a
fresh `observe`/`find` pair rather than from a return code, with the honest
limit of section 4 recorded in the report: verified-against-a-spoofable-read
is not the same word as verified.

**Not in this item:** the fallback ladder rungs and the target record are item
521's; accumulated state inside the target application is item 546's; the
substrate is upstream.

## 11. What is not verified

Written down so a reader does not infer more than was measured.

- **Slice 1 does not stop a click.** It stops a program from claiming a click
  is clean. An `unknown` or `irreversible` UI crossing is still admitted with
  no confirmation, for the reason in section 6. That is the largest gap in
  this item today and it is deliberate, not an oversight.
- **The check-to-use race is open.** Section 4 names it and item 521's slice 4
  is the fix. Nothing in the tree resolves a UI target into a handle today, so
  every phase boundary re-resolves by name.
- **There is no transaction.** No phase list executes, nothing runs LIFO, and
  no `uncompensated` value is produced anywhere. Section 3's table is a
  division of responsibility, not an implementation.
- **The claim that the residue report reads an extern-declared `compensate`
  was read, not re-measured here.** It is item 254's own finding and its
  fixture (`tests/fixtures/erase_net.rvl`) is the evidence. Slice 1 does not
  add a test that a refused UI `compensate` would have reached that report,
  because the refusal means no such program exists to measure.
- **The class assignments are judgements about GUI operations in general.**
  `ui.text` is compensatable in an application where a field can be restored
  and not in one where typing triggers a search that is itself an effect. The
  registry takes the conservative reading per verb; a finer answer needs a
  finer verb, not a per-program override.
