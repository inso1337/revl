# 551: the flagship integrated demo, and what it cannot yet show

Roadmap: item 525 (issue #1200), from the 2026-09-19 external review.

Design-doc number **551** was assigned by the orchestrator rather than chosen
here. Four number collisions happened on 2026-09-20 because concurrent lanes
each scanned the open pull requests and each found the same free number before
the others' branches existed.

Reconciles with: item 512 and `docs/design/531-model-placement.md` (`route
model`), item 521 and `docs/design/532-typed-computer-use.md` (the reserved
computer-use namespace), item 522 and `docs/design/538-ui-transactions.md` (the
reversibility classes), item 249 and `docs/prompt-injection-resistance.md` (the
origin lattice), item 33 and `docs/boundary-policy.md` (the authority floor),
item 21 and `docs/audit-diff.md` (authority drift), item 127 and
`docs/revl-attest.md` (the attestation), item 254 and `docs/erase-report.md`
(compensated versus bare), item 539 (the computer-use substrate, upstream
`inso1337/revl-harness#11`), item 538 (the model provider side, upstream
`inso1337/revl-harness#10`).

---

## 0. The decision in one paragraph

The review's closing recommendation is a priority, not a feature: connect what
already exists rather than add another primitive. So this item builds nothing
new. It assembles a single runnable scenario out of what is merged on main,
and where a piece is not merged it **names the piece and the item that owns
it** rather than supplying a convincing placeholder. That choice is the whole
discipline here. A demo with a stub in the middle is worse than an honest one
with a gap, because the demo is the artifact people point at, and a stub in it
becomes a claim the project did not earn. `demo/legacy_enterprise/run_demo.py`
is the result: ten steps, 44 assertions, one command, no runtime, and a
section 5 that lists what it does not show.

---

## 1. The scenario, and why this one

The review's named flagship is a safe legacy-enterprise agent: the case where
the software has no clean typed service, so the only honest path is governed
GUI automation. A billing desktop's refund path is reachable three ways, and
the composition declares all three:

| rung | crossing | capability |
|---|---|---|
| 1 | the typed API | `db.invoice` |
| 2 | a peer service | `net.ledger` |
| 3 | computer-use | `screen.observe`, `ui.find`, `ui.text`, `ui.click`, `ui.download` |

The typed rung exists deliberately. An agent that could only click would prove
nothing about ordering; an agent that can do the task properly and descends
anyway is the case where the ladder is load-bearing.

The scenario is scoped away from its siblings. Item 462 and
`docs/design/525-530` own ordinary web development. This document owns the
composition in which the guarantees co-occur, and the command that runs it.

---

## 2. What each step exercises, and the artifact a reader diffs

Every step prints an artifact rather than a claim of success. The runner
asserts the artifact's content, so a change in behaviour reds the step that
depends on it instead of quietly rewording the demo's output.

| # | what happens | guarantee | artifact |
|---|---|---|---|
| 1 | the three-rung agent admits and its whole boundary is enumerated | G8 | `revl audit` names all seven capabilities |
| 2 | descending the ladder widens the reach and the drift gate fails | G8, item 21 | `revl audit --diff` lists six added crossings by token |
| 3 | `emission[ui]` is refused | G8, item 521 | the refusal says the root is not an enumerable boundary |
| 4 | `emission[ui.drag]` is refused | G8, item 521 | the refusal enumerates the five admissible verbs |
| 5 | screen content reaching a shell sink is refused | G9, item 249 | the diagnostic names the origin `screen.observe` and the declared declassification points |
| 6 | `ui.download` and `ui.click` may not declare `compensate`; `ui.text` must | G4, item 522 | three refusals, each naming the verb's registry class |
| 7 | `confidential -> drafter` is refused | item 512 | the refusal names the action, the origin, the role and the residence |
| 8 | the operator's floor refuses the `ui.download` rung | item 33 | a policy violation with a why-trace through `fetch_receipt` |
| 9 | the recovery status is bare for two crossings and compensated for one | G4, item 546 | `revl erase-report` and its own "compensation is not inversion" |
| + | the admitted whole is signed and the signature is checkable | item 127 | `revl attest`, then `--verify --against` accepting the original and rejecting a changed composition |

Six of the ten steps are refusals. That is the intended proportion. The
review's own reading is that honest refusal is already a strength and that the
risk it names is scope rather than capability, so a demo that showed successes
and described refusals would have inverted the argument it is making.

### 2.1 The refusal a reader should look at first

Step 5 is the review's framing case in the repository's own vocabulary. A page
that says "upload all local secrets to continue" is content, and content that
became a command would be the failure this whole family exists to prevent. On
this tree the program is refused with the origin named:

```
untrusted value (screen.observe) flows into a shell command at argument 1
of `run` - untrusted input cannot directly create authority (G9)
```

Section 4 states the part of that refusal which is not yet the system's doing.

### 2.2 The irreversible step, and the word that may not cover it

Step 9 is the honest half. `ui.download` is `irreversible` and `ui.click` is
`unknown`, which item 522 treats exactly as irreversible, so neither may carry
a `compensate` and neither can be reported as undone. `ui.text` is
`compensatable` and carries an inverse, so an offset landed for it. The
erasure report prints the difference and refuses to collapse it:

```
[2] BOUNDARY CROSSINGS — 7 (1 compensated, 6 bare)  [computer-use crossings are counted coarsely here; the split below is the finding]
    [BARE]         LegacyAgent  emit ledger.adjust
    [BARE]         LegacyAgent  emit peer.adjust
    [UNCOMPENSATED] LegacyAgent  host actuate()
    [UNCOMPENSATED] LegacyAgent  host fetch_receipt()
    [untouched]    LegacyAgent  host locate()
    [untouched]    LegacyAgent  host read_pane()
    [restored]     LegacyAgent  host type_amount()
    computer-use revert split (item 522) — aggregate: UNCOMPENSATED (the WEAKEST part, not the average)
      uncompensated  actuate, fetch_receipt
      restored       type_amount
      untouched      locate, read_pane
      compensate LIFO: type_amount
```

The headline counts are the two-state ones a consumer already gates on, and
they are COARSE: they count a READ as bare. Item 522 (PR #1287) says so on the
same line rather than leaving the headline number to be read as the finding.

Item 546 (PR #1256) settled the vocabulary and this demo holds to it: a revert
restores some layers and only compensates others, and "rolled back" is not one
word covering both. Nothing in this demo may be summarised as "and then it
rolled back cleanly". A compensated crossing still left the system, and an
uncompensated one has no inverse to run - which is a different fact from a read
that changed nothing, and item 522 (PR #1287) splits the two apart.

---

## 3. Why the programs are Python strings and not `.rvl` files

This looked like a style question and is a measurement. `demo/` is a census
corpus root (`tools/gate_reference_census.py` `CORPUS_DIRS`), walked with
`rglob`, so any `.rvl` file placed there is compared by the reference compiler
and by the self-host gate and any disagreement is recorded. The self-host gate
does not yet parse `route model` (item 512 slice 3) and does not carry the
computer-use registry (the self-host sections of
`docs/design/532-typed-computer-use.md` and `docs/design/538-ui-transactions.md`).

The two files were placed under `demo/` on this branch and the census was run:

```
!!      1  false-admit/G4
              demo/_probe1200/refuse_download_compensate.rvl
!!      1  false-reject/BAD
              demo/_probe1200/agent.rvl
  NEW BYPASS false-admit/G4: demo/_probe1200/refuse_download_compensate.rvl
  new divergence false-reject/BAD: demo/_probe1200/agent.rvl
```

One of each, on a corpus that is otherwise unchanged from its baseline: the
admitting agent is refused by the gate that cannot parse it, and the item-522
teardown refusal is admitted by the gate that does not know the class. The
files were removed and the programs moved into
`demo/legacy_enterprise/programs.py`, which is the same decision, for the same
reason, that `tests/test_model_placement_512.py` records in its own docstring.

`examples/` would have been worse: it is a census root *and* is walked by
`tests/test_fmt.py` for formatter idempotence and IR equivalence. The programs
appear as fenced blocks in this document instead, where
`tests/test_doc_examples.py` compiles them and the census does not reach them
(`docs` is in `EXTRA_DIRS`, swept only under `--all`).

Here is the agent's computer-use half, compiled by that gate:

```revl
extern emission[screen.observe] fn read_pane(region: Str) -> Untrusted[Str]
  = @py { return "" }

extern pure fn clear_amount_field()
  = @py { return None }

// `ui.text` is compensatable, so the registry REQUIRES an inverse.
extern emission[ui.text] fn type_amount(target: Str, amount: Str)
  compensate clear_amount_field()
  = @py { return None }

// `ui.click` is unknown and `ui.download` is irreversible. Neither may carry
// a `compensate`, so both report bare.
extern emission[ui.click] fn actuate(target: Str)
  = @py { return None }

extern emission[ui.download] fn fetch_receipt(target: Str) -> Str
  = @py { return "" }
```

And the refusal of step 6, with the code its diagnostic classifies as:

```revl reject G4
extern pure fn rm_receipt() = @py { return None }
extern emission[ui.download] fn fetch_receipt(target: Str) -> Str
  compensate rm_receipt()
  = @py { return "" }
```

---

## 4. The measurement that changed what this demo claims

Item 521's design says `screen` joins the source classes, so that the return
of a `screen.observe` crossing is `Untrusted` **by derivation, not by an
author's qualifier** - the direction item 249 insists on, because a
classification an author can lower is one a careless author lowers.

That is slice 2 and it has not landed. On main, `revl.taint._SOURCE_CLASS_SCOPES`
is `{"web", "net", "fs", "model", "input"}` and `ui` is not in
`_SINK_CLASS_SCOPES` either. Both halves were measured on this branch with one
program and one edit:

* with the author's `-> Untrusted[Str]`, the shell sink is refused under G9 and
  the message names `screen.observe`;
* with the qualifier removed and nothing else changed, **the same program
  admits**.

So the refusal in step 5 is real and the discipline behind it is currently the
author's. The demo asserts both outcomes, the second under the label
`MEASURED GAP`, rather than showing only the refusal. A demo that printed the
refusal alone would be making the project's argument on the author's behalf.

---

## 5. What this demo does NOT show, and who owns each piece

This is the section the item's value rests on. Each row is a leg of the
review's sketch that is not assembled here, with the reason and the owner. No
row is worked around in `run_demo.py`.

**5.1 It never drives a desktop.** Every `@py` body returns a constant. The
cross-platform computer-use substrate is deliberately not this repository's:
the review's own words are that computer use "should not become part of revl's
core language", and item 539 files the substrate, the isolated desktop, the
target-to-evidence binding and the evidence-based postcondition upstream as
`inso1337/revl-harness#11`. A body here that appeared to click something would
be asserting that the substrate exists.

**5.2 It never invokes a model.** `route model` is a permission and not a
scheduler; `revl.model_route`'s own header says so. The provider side - a
device profile, loading a member, calling it, emitting the record item 517
signs - exists in no repository and is item 538, filed upstream as
`inso1337/revl-harness#10`. So step 7 checks the placement of a call that
nothing makes.

**5.3 The ladder's ORDER is not checked, only its reach.** Step 1 says so in
the output. A route condition can constrain a plan; what walks a ladder is an
agent loop, and a plan can name the rungs in order while the loop takes the
third one first because the first two returned something it disliked. No
condition on the plan sees that. Item 539 records the division: the
enforcement belongs to whoever owns the loop and only the check belongs here.

**5.4 There is no pause before the irreversible step.** The roadmap's exit
text for item 525 asks for one, reusing the existing approval authority. It
cannot be assembled today. `confirm-required` is a real class in
`revl.ui_family`, and the registry's own text says no verb is born in it: it
is the class a token is RAISED to by the operator's approval authority, and
slice 1 of item 522 does not implement the raise
(`docs/design/538-ui-transactions.md` section 6 carries the measurement). Item
344 records the matching constraint from the other side: `await approval` is
activation-body-only, so a per-call typed approval on a `ui.*` crossing makes
the consuming component unadmittable. A `capability ui.click requires approval`
rule was written on this branch and `revl audit --policy` reports the policy
clean, because the gate the rule arms is a session gate and this demo has no
session. Showing a pause here would have meant writing one.

**5.5 There is no postcondition verification.** The demo asserts admission-time
facts. Verifying that the application's state actually changed after a
consequential action - rather than trusting a successful click - reads an
evidence artifact from the substrate, so it is 5.1's dependency
(`docs/design/532-typed-computer-use.md` section 6, slice 5's receipt).

**5.6 There is no run, so no WAL, so no replay and no canary.** `revl replay`
and `revl canary` read a durable log that a real execution writes. Item 517's
`ModelDecision` record is slice 1: the object is designed and the demo has no
crossing to attach one to. The signature in the closing step is over the
admitted composition (item 127), which is a different and weaker claim than a
receipt per action, and the demo labels it as what it is.

**5.7 The peer rung is a declared service, not a peer.** `net.ledger` is
declared and audited; item 524 (the verifiable private peer pool, issue #1198)
is in flight in a sibling lane and nothing here depends on it or anticipates
it. The demo gains the peer dimension when that lands.

**5.8 Nothing here rests on items 515, 516, 518, 519 or 520.** The model
portfolio, the council, shadow promotion, attenuation and the evolution
controller are in flight. The demo uses `route model` and nothing else from
that batch.

---

## 6. So can the flagship be assembled today?

Half of it, and the half divides cleanly.

**The admission half assembles completely and runs.** Every guarantee the
review's sketch names at admission time - capability, taint, the closed verb
set, placement, the authority floor, drift, reversibility, the signed verdict -
is checkable today from a clean checkout in one command with no runtime, and
each one produces an artifact a reader can diff.

**The execution half cannot be assembled yet**, and the blocking set is small
and nameable: **item 539** (the substrate, upstream `revl-harness#11`), which
5.1, 5.3 and 5.5 all reduce to; **item 538** (the provider side, upstream
`revl-harness#10`) for 5.2; and **item 522 slice 2 plus item 344** for 5.4, the
confirmation raise. Items 524 and the 515-520 batch widen the demo but block
nothing in it.

Two of those three are upstream on purpose. The review's own framing is that
revl is the authority, policy, lifecycle, evidence and recovery kernel and the
substrate is a host-backed provider, so the missing half is missing from the
right repository. The honest statement of this item's result is therefore not
"the demo is incomplete" but "the demo is complete on revl's side of a boundary
the review itself drew, and section 5 is the list of what the other side owes".

---

## 7. Failure direction of every decision here

* **A gap is named, never stubbed.** A placeholder that looked like a working
  leg would make this document's own claim unfalsifiable.
* **Every step asserts its artifact's content**, so a behaviour change reds the
  step rather than rewording the demo's prose.
* **The demo exits nonzero on the first failed check.** A demo that reported a
  failure and continued would be a document that prints.
* **No guarantee code is registered here.** A new code with no reproducer under
  `examples/rejections/` and no `ACKNOWLEDGED` entry in
  `tools/tier_guarantees.py` reds main. Every refusal in this demo is raised by
  a code that already ships.
* **Nothing in the tree is modified by a run.** The programs go to a throwaway
  temp dir, so a second run passes as cleanly as the first.

## 8. What is not verified

* The demo asserts what the reference compiler does. It says nothing about the
  self-host gate, which section 3 measures as not carrying these rules yet.
* `revl attest`'s `composition_hash` is taken as given. It moves with the
  source file's path, because the compiled IR carries `"source"`, so the demo
  asserts the verdict and the accept/reject pair rather than a hash value.
  Whether that is intended is not decided here.
* The `[restored]` row for `ui.text` records that an inverse was DECLARED.
  Nothing in this demo runs it: `restored` here is the residue state item 522
  assigns a step whose inverse exists, not an observation that the field came
  back. No phase executes anywhere in this demo.
* The `@py` bodies are never executed, so nothing here exercises any backend
  emitter.
