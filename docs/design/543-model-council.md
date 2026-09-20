# 543: A model council with role-bound members and typed disagreement

Roadmap: item 516 (issue #1190), from the 2026-09-19 external review. Slice 1
is LANDED with this note; slices 2 to 5 are designed here and not written.

Builds on: item 512 and `docs/design/531-model-placement.md` (the `model role`
declaration each member names, and section 9 of that note, which decided for
this item that roles are program-level precisely so several can be bound in one
aggregation), item 514 (the origin ceiling, which is what makes placing the
members separately mean something), item 517 (the signed decision object, whose
crossing key a council reuses rather than inventing a second correlation),
item 249 and `docs/design/249-taint-provenance.md` (the origin lattice),
item 471 (multi-party human approval, which this is NOT).

---

## 0. The decision in one paragraph

Asking one model to be confident is not evidence. Where an action is
consequential the useful shape is several models with declared functions and an
aggregation that is written down, and the one thing that must never happen is
the council answering anyway when the members disagree, because a caller
reading the word "council" believes several agreed. So the council does not
return the answer type. It returns a closed sum whose disagreement constructor
carries no answer, the aggregation rule is a declared word from a closed
vocabulary rather than a function the program supplies, the rule's floor is
counted over the members the program DECLARED rather than over the ones that
happened to answer, and the two spellings that would resolve a disagreement
toward an answer parse so that they can be refused by name. Slice 1 is the
declaration half, checked, exactly as item 512's slice 1 was.

---

## 1. The gap

A component that wants two opinions today writes two model calls and combines
them in ordinary code:

```revl fragment
provide out {
  fn review(plan: Str) -> Str =
    agree(emit fast.complete(plan), emit slow.complete(plan))
}
```

Three things are wrong with that and none of them is a missing runtime.

1. **The combination is invisible.** `revl audit` sees two crossings and no
   relationship between them. Nothing records that the second was meant to
   check the first, so nothing can say the check was skipped.
2. **The failure direction is unconstrained.** `agree` is an ordinary function
   the program wrote, and nothing states what it does when the two answers
   differ. Returning the first is as available as refusing, and it is the
   version that ships, because it is the version that never blocks. A reviewer
   sees two model calls and believes the answer was corroborated. This is the
   whole item: **a council that degrades to "whichever member answered" is
   strictly worse than one model**, because it adds confidence without adding
   evidence.
3. **The members are not placed.** Item 512 places a model call by the origin
   of what the action is given. Two calls written inline are two placements the
   program can get inconsistently right, and nothing states that the second is
   the one that may read what the first may not.

Repeat sampling on one model is not the fix, and the issue's own alternatives
section says why: same weights, correlated errors, no separate placement.
Neither is a prompt that asks the model to self-critique, which is not a role,
not bound to evidence, and not checked.

---

## 2. The surface

One declaration, at the program level, spelled with contextual identifiers so
the lexer's `KEYWORDS` table and the self-hosted lexer that mirrors it need no
sync. `model` already heads a contextual declaration in the shape
`model role NAME <residence>`; this adds the second shape `model council NAME {`
and nothing else about `model` changes.

```revl
model role edge   on_device
model role review on_device
model role vast   off_device

model council Release {
  proposer  -> vast,
  adversary -> edge,
  verifier  -> review,
  aggregate unanimous
}

service Plan { fn review(plan: Str) -> Str }

component Reviewer provides out: Plan {
  provide out { fn review(plan) = plan }
}
```

A member is a pair of words and the pair is the point. **The function** is what
the member is for: `proposer`, `adversary`, `verifier`, a closed vocabulary.
**The role** is an item-512 `model role`, which is where the member's call runs.
Separating them is what lets a council put a local adversary beside a cloud
proposer, and it is why the roles have to be program-level: a council in one
file binds roles that several components also route to.

`aggregate <rule>` is written inside the braces, as a further item in the same
comma-separated list rather than as a clause after the closing brace. That
placement is deliberate and section 12 says which sibling lane it was chosen
around.

The full grammar of the aggregation item:

    aggregate <rule> [quorum <basis>] [on_tie <outcome>]

`quorum` and `on_tie` are optional and default to `declared` and `split`, which
are the fail-closed readings. Omitting them is the same as writing them; the
reason both exist as words at all is that the spellings an author reaches for
when they want the other reading, `quorum answered` and `on_tie allow`, must be
refusable **by name** rather than as a syntax error. That is the discipline
item 515 uses for `*` on the right of a route arrow: the author who asks for the
dangerous thing should be told why, not told that the parser did not expect a
token.

---

## 3. What a member's answer IS, at the type level

This section and the next are the item. Everything else is bookkeeping.

A member does not answer with `T`. It answers with `Answer[T]`, a closed sum
with three constructors:

| constructor | meaning |
| ----------- | ------- |
| `Says(T)` | the member was asked and produced a value of the boundary type |
| `Abstains` | the member was asked and declined, or produced nothing the item-257 boundary admits |
| `Unreachable` | the member could not be asked at all |

`Abstains` and `Unreachable` are separate constructors although the aggregation
treats them the same way (section 5). They are separate because the evidence
must distinguish them: a member that answered "I will not say" made a
statement, and a member whose host was down did not, and a record that conflates
the two cannot be used to decide whether the council is structurally broken or
merely unlucky.

The council does not answer with `T` either. It answers with `Aggregate[T]`:

| constructor | meaning | carries |
| ----------- | ------- | ------- |
| `Agreed(T)` | the declared rule named exactly one value | that value |
| `Split(Dissent)` | every declared member was asked and the rule named no value | the dissent record |
| `Inquorate(Dissent)` | fewer members answered than the rule's floor, so the rule did not apply | the dissent record |

**`Split` carries no `T`.** That sentence is the type-level statement of the
whole item, and it is the reason this is a type and not a convention. There is
no total projection `Aggregate[T] -> T`. The only constructor that holds an
answer is the one that means the members agreed, so a caller that wants a `T`
has to pattern-match, and the arm it has to write for `Split` cannot be
satisfied by reading a value out of the payload, because the payload is not of
that type.

`Dissent` is the per-member record, in declared member order:

    Dissent = [ { function, role, residence, answer: Answer[Digest] } ]

The answers are carried as **digests**, not as values of `T`. Two reasons, and
the second matters more than the first.

1. A `T` inside `Split` is a `T` in hand, and the next author writes
   `split[0].answer`. That is `aggregate first` spelled at the call site
   instead of in the declaration, where the checker cannot see it. Making the
   payload a digest means the shortcut does not typecheck, which is a stronger
   guarantee than a rule that forbids it, because it needs no rule.
2. A member's answer carries the origin of what the member was given (item
   249). A `Split` full of raw completions would carry the cloud proposer's
   text to wherever the caller logged the dissent. The digest binds the answer
   without moving it, which is item 517's decision verbatim: hashes are the
   binding, and retaining content is an explicit decision that carries the
   taint.

### 3.1 Disagreement is a value, not a fault

`Split` is returned, not raised. A fault would be catchable generically, and a
generic catch around a council reads, to the code after it, as "the council did
not object". A value forces the caller's match to name the case. The same
argument decided item 522's `uncompensated`: an honest outcome that the caller
must handle beats an exception that the caller can handle by accident.

---

## 4. What happens when no aggregation rule applies

It is a named outcome, and the name is `split`.

The three admitted rules are closed vocabulary, and each of them either names
exactly one value or names none:

| rule | names a value when | otherwise |
| ---- | ------------------ | --------- |
| `unanimous` | every declared member answered and every answer is the same | `Split` |
| `majority` | strictly more than half of the DECLARED members gave one same answer | `Split` |
| `veto` | the adversary said `deny` (the value is the denial), or the rest were unanimous | `Split` |

None of the three is a tie-break. `majority` on a 1-1-1 three-way split names
no value; that is not an error and not a fallback, it is `Split`. `veto` is
included because it is the one rule that can only ever move the outcome toward
refusing, which makes it safe by construction, and it is the rule an adversary
member exists to serve.

The `on_tie` word names what the council does in that case, and its vocabulary
has exactly two members: `split`, the default, which returns the dissent; and
`deny`, for a council that decides inconclusive means refuse. **There is no
admitting outcome**, and the spellings for one are enumerated so that the
refusal can be precise:

```revl reject G-MODEL-PLACE
model role edge on_device
model role aux  on_device
model role vast off_device

model council Release {
  proposer  -> vast,
  adversary -> edge,
  verifier  -> aux,
  aggregate majority on_tie allow
}
```

    `on_tie allow` in model council `Release` admits when the members disagree
    (G-MODEL-PLACE)

That is the roadmap's own exit test for the declaration half, and its hint
names both admitted outcomes so the fix does not require reading this note.

The same shape refuses the rule that picks a member rather than aggregating:

```revl reject G-MODEL-PLACE
model role edge on_device
model role vast off_device

model council Release {
  proposer  -> vast,
  adversary -> edge,
  aggregate first
}
```

    `aggregate first` in model council `Release` resolves disagreement toward
    one member's answer (G-MODEL-PLACE)

---

## 5. Abstain or block, and why

**A member that fails or times out abstains.** It does not block, and the
council call does not fault.

The argument against blocking is availability, and it is not a comfort
argument. If any single member can stop the decision, then the member most
exposed to an adversary, which is normally the cloud proposer with the longest
tail latency, becomes a denial-of-service lever on every consequential action
the program takes. A council that blocks is less available than the one model
it replaced, and a council that is less available than one model gets deleted,
which returns the program to the shape this item exists to improve on. Fail
closed is not the same as fail hard.

But abstention must not be able to shrink the council into a quorum, and that
is the half that carries the weight:

**The rule's floor is counted over the DECLARED members, never over the ones
that answered.** `unanimous` over three declared members needs three agreeing
`Says`. Two agreeing with the third abstaining is not unanimity, it is
`Inquorate`. `majority` over four declared members needs three, not two of the
two that answered.

This is why `quorum answered` parses and is refused. It is the exact spelling
of the roadmap's second refusal, "an aggregation that ignores an unreachable
member is refused", and the diagnostic says what goes wrong: a floor counted
over the answering set lets the council shrink until the survivors agree, so an
adversary who can make one member unreachable can manufacture unanimity among
the rest.

Two consequences worth stating because a reader will look for them.

* **The adversary's silence is not absence of objection.** It follows from the
  floor rule with no special case: an abstaining adversary leaves the declared
  set unmet, so the result is `Inquorate` under every rule, including `veto`.
  This is the property a "the adversary did not object, so proceed" reading
  would have destroyed, and it is worth having for free rather than as an
  eleventh exception.
* **`Inquorate` is not `Split`, and the difference is actionable.** `Split`
  means the council worked and the members disagree, which is information about
  the question. `Inquorate` means the council did not work, which is
  information about the deployment. A caller may reasonably escalate the first
  to a human approval (item 471) and retry or alarm on the second; conflating
  them would make both untreatable. The `Dissent` record names which member was
  silent and whether it abstained or was unreachable.

---

## 6. Placing the members separately, and the council's own ceiling

This is what items 512 and 514 give the council, and it is why the council is
built on roles rather than on a list of model names.

Each member names its own `model role`, so each member has its own residence
and item 514's origin ceiling applies **per member**. A council with a cloud
proposer and a local adversary is two placements, and a refusal about the
proposer names the proposer.

The council's own ceiling is then the one number slice 2 reads, and it is
computed in the direction that fails closed:

> **A council's residence is the MOST permissive of its members', not the
> least.** `Council.residence` is `off_device` if any member is `off_device`.

Giving an input to a council gives it to every member, so a council with one
cloud member is a cloud placement for that input. Computing the ceiling the
other way round, as "the council is on_device because it has an on-device
adversary", is the fail-open reading, and it is the specific error the brief for
this item warns about: a council whose members are all cloud-placed gives a
confidential input to every one of them while reading, to a reviewer, like a
confinement structure.

### 6.1 The limit: one input, asked of every member

Slice 1 asks every member the same question with the same input. That is why
the ceiling is a join.

The sentence "a local adversary can read an origin the cloud proposer is not
allowed to see" is literally true only once a council can give **different
inputs to different members**, which needs a per-member input declaration and is
slice 4 below. What slice 1 and slice 2 deliver is the half that has to come
first and is useful on its own: the members are placed separately, so the
ceiling is decided per member and the refusal names the member whose residence
made it a refusal, rather than refusing "the council" and leaving the author to
work out which of three placements was the problem. Until slice 4, the
conservative join is enforced, which is the fail-closed direction of the same
rule. Stated here rather than implied by silence, for the same reason item 512's
section 2.1 states its slice-1 limit.

---

## 7. Several decisions, one crossing: the item 517 key

A council produces several model decisions for one crossing, so it has to say
how they relate to item 517's key rather than invent a second correlation. It
invents none.

Item 517 binds a decision to `(component, step_index)`, which is
`revl.wal.model_decisions`' own key, and carries `role` and `residence` as
declared fields of the record. A council's crossing is **one step**. Its N
members therefore produce N records that share `(component, step_index)` and
differ in `role`.

That is unique, and it is unique because of a declaration rule rather than by
luck: **two members of one council may not name the same role** (section 8,
rule 7). So `(component, step_index, role)` identifies a member's decision
exactly, with no council id, no member ordinal and no new correlation field.
The rule was written for a different reason, that two members on one placement
are one model answering twice under two names with correlated errors, and this
is the second thing it buys.

The council's own outcome needs no field either. `Agreed`, `Split` and
`Inquorate` are **derivable** from the member records under the key plus the
declaration, by replaying the declared rule over the `candidates`/`chosen`
digests. That is better than recording it, because a recorded verdict can
disagree with the records it summarises and a derived one cannot: a council
cannot claim agreement that its own evidence does not show. Item 517's
per-record `outcome` vocabulary (`validated`, `refused`, `exhausted`) stays what
it is, a statement about that member's own call, and is not overloaded with the
council's verdict.

---

## 8. What the checker decides

All of it lives in `src/revl/model_council.py`, beside the rules, the way
`revl.model_route` holds item 512's. The parser reads the shape and validates
nothing.

Every refusal carries `G-MODEL-PLACE`. **No new guarantee code is registered.**
The council is the placement family's second construct, not a second family:
the code already reads "a model role declared `off_device` never receives a
confidentiality origin, and an action reaches only the roles its `route model`
block names", and every rule below is a rule about which roles a declaration
binds and how. Registering a new code would also have needed either a
reproducer under `examples/rejections/` or an `ACKNOWLEDGED` entry in
`tools/tier_guarantees.py` for item 523's generated tier matrix, and adding a
family for a slice that adds no new class of refusal is not worth either.

| # | The decision | Direction |
| - | ------------ | --------- |
| 1 | a council name is declared once | closed: two member sets under one name would be resolved by declaration order |
| 2 | a council name is not also a model role name | closed: slice 2's route arm names either, so one word may not be both |
| 3 | a member's function is in the closed vocabulary | closed: a typo is a refusal, never a member with no job in the aggregation |
| 4 | a function is declared at most once per council | closed: two members with one function make the count depend on order |
| 5 | a member's role is a declared `model role` | closed: an undeclared name has no residence, so the member cannot be placed, and unplaceable is refused rather than assumed |
| 6 | no two members name the same role | closed: one model answering twice under two names is correlated error, and it would also break section 7's key |
| 7 | a council declares at least two members | closed: one member wearing the word "council" is the misrepresentation the item removes |
| 8 | a council declares a `proposer` | closed: with nothing to propose, the rule has no value to name and every result is inconclusive |
| 9 | a council declares exactly one `aggregate` | closed: none is the silent pick; two is declaration order deciding between rules that can disagree |
| 10 | the rule is in the closed vocabulary, and `first`/`any`/`fastest`/`cheapest`/`best`/`random` are refused by name | closed: a rule that picks a member is not an aggregation |
| 11 | `aggregate veto` declares an `adversary` | closed: a veto clause naming a member that does not exist reads as a protection the council does not have |
| 12 | `quorum` is `declared`, and `answered`/`reachable`/`available`/`responding` are refused by name | closed: a floor over the answering set lets an unreachable member disappear |
| 13 | `on_tie` is `split` or `deny`, and `allow`/`admit`/`proceed`/`accept`/`first`/`any` are refused by name | closed: the roadmap's exit test, and the one direction that must never exist |

Rules 10, 12 and 13 share a shape worth naming, because it is the shape this
repository keeps finding on the wrong side: each of them refuses a spelling that
**reads as a safety feature and is not one**. `on_tie allow` reads as a
considered decision about ties. `quorum answered` reads as pragmatism about
flaky hosts. `aggregate first` reads as a latency optimisation. All three
produce a caller who believes several models agreed, which is the one outcome a
council must never manufacture, so all three are refused with the reason rather
than parsed into a warning.

---

## 9. Why this writes no IR

Like item 512's slice 1, a `model council` declaration contributes nothing to
the IR. An admitted program is byte-identical to the same program with the
declaration deleted, no emitter changed, no golden moved and the manifest is
unchanged.

This is honest rather than an accounting trick, for the same two reasons item
512 gives. The declaration grants no ability, so there is no behaviour to be
surprised by: a program that declares a council and then calls a model reaches
exactly the model it reached before. And every rule in section 8 points toward
refusing, so the worst outcome available from the declaration alone is a
program that does not compile. What is genuinely absent until slice 2 is any
binding between a council and an action, and section 12 states it in the slice
plan rather than leaving it to be inferred.

---

## 10. Not item 471, and the sentence that keeps them apart

Item 471 and item 509 are multi-party human approval. The votes there come from
operators with identities, and a vote is a statement about consent.

A council member is a model. It has a function and a placement and **no
identity**, it consents to nothing, and its agreement is not approval. The two
constructs look alike from a distance, both being "several answers combined
under a stated rule", and conflating them would be a real harm in both
directions: a council reading as consent would let a program claim human
sign-off it never had, and an approval reading as a council would let a model
stand in for an operator.

Three things keep them apart mechanically rather than by convention. The
vocabularies do not overlap: `proposer`, `adversary` and `verifier` are not
operator roles, and nothing in this construct names a principal. The outcome
type is different: an approval yields a consent record bound to an identity, a
council yields `Aggregate[T]` bound to nothing. And the escalation direction is
one-way: a `Split` may be escalated TO a 471 approval, and a 471 approval may
never be discharged BY a council.

---

## 11. Non-goals

* **A runtime aggregator.** Slice 1 is the declaration. `Aggregate[T]` is
  specified in section 3 and not written.
* **Naming a model.** A member names a role; a role is bound to a member by
  configuration (item 512 section 5). No vendor, weights hash or endpoint
  appears in a revl document.
* **Scheduling the members.** Whether the three calls run in parallel, and what
  a member costs, is item 515's. The council states the rule, not the plan.
* **A user-supplied aggregation function.** The rule is a word from a closed
  vocabulary on purpose. An arbitrary function is a place to write
  `return answers[0]`, and a checker cannot refuse what it cannot see.
* **Weighting members.** A weighted vote is a tie-break with extra steps, and
  the weights are exactly the knob that would be tuned until the council
  agrees. If a member's answer should count for less, that is a statement about
  whether it belongs in the council.
* **Retrying a member.** Repeat sampling of one member is the alternative the
  issue rejects. A retried member is the same weights answering twice.
* **Widening anything.** The item adds refusals and removes none. No program
  that compiles today stops compiling, and a council grants no capability; that
  direction is item 519's.

---

## 12. Where it lands, and what was assumed from the sibling lanes

| File | Change |
| ---- | ------ |
| `src/revl/parser.py` | `CouncilMember`, `AggregateClause`, `ModelCouncilDecl`; `Program.model_councils`; one contextual dispatch arm; `model_council_decl()` and `_council_aggregate()` |
| `src/revl/model_council.py` | NEW. The vocabularies, `Member`, `Council`, `check()`, and all thirteen refusals |
| `src/revl/lower.py` | `_model_council.check(program)` in `_check_and_lower`, after the route check, writing no IR |
| `src/revl/compiler.py` | `model_councils` rides the declaration closure across modules, the `model_roles` rule |
| `tests/test_model_council_516.py` | NEW |

No emitter, no manifest, no IR, no lexer, no crate, and no diagnostics
registration. `tools/build_gate_crate.py --check` reports in sync, which is the
authoritative answer and not an inference from the file list.

Three sibling lanes were extending the same surface while this was written, and
the `model role` declaration is the surface they share. The one collision
surface their lanes named is **clause order after the residence**, so the
assumption this note makes about each is stated here.

* **Item 515 (`agent/1189-model-portfolio`)** adds an optional
  `device <class> memory <int> quant <tag>` clause immediately after the
  residence on `model role`, and an ordered candidate set
  `<origin> -> a | b | c` on a route arm. Assumed: `model role`'s post-residence
  slot belongs to that lane. **This item adds nothing after a residence.** A
  council is its own top-level declaration, so the two grammars do not meet;
  the only shared line is the contextual dispatch arm that reads `model`, and
  that lane does not touch it.
* **Item 519 (`agent/1193-model-in-attenuation`)** adds an optional
  `reaches [...]` clause, also immediately after the residence. Assumed: the
  same slot, and that 515 and 519 will settle the order of their two optional
  clauses between themselves. Nothing here depends on which order they pick,
  because a council reads a role through `revl.model_route.roles()`, which
  returns a `Role` with `name`, `residence` and `line` and is unchanged by
  either lane.
* **Item 518 (`agent/1192-shadow-promotion`)** relates the two roles one route
  arm names, incumbent and successor, and adds no syntax of its own. Assumed:
  a council and a shadow pair are different relations over the same roles and
  do not have to agree. A council is a set of members answering one question
  under one rule; a shadow pair is one question answered twice so the answers
  can be compared. Both may name the same role, neither constrains the other,
  and the two must not be merged: the shadow comparison is a promotion gate
  with a recorded verdict, while a council's `Split` is an outcome returned to
  the caller.

The `aggregate` clause is written **inside** the council's braces rather than
after the closing brace for this reason. A post-brace clause would have created
a second "what comes after the closing token" slot in the same family, a week
after the first one produced a three-way collision. Inside the braces the
council's own grammar is self-contained and extensible without touching
anything the other three lanes read.

---

## 13. The self-host question

`selfhost/*.rvl` is a second implementation that must agree with the reference,
and its oracles catch divergence but not a missing feature. So the question is
not whether the self-host implements `model council`, which it does not, but
whether it can SILENTLY ADMIT a program the reference decides.

Measured, on the gate built from `selfhost/lower.rvl` at this branch:

| program | reference | gate |
| ------- | --------- | ---- |
| the component with no council (control) | admits | `''` (admits) |
| a council with `aggregate unanimous` | admits | `BAD\|unexpected token at top level` |
| the same council with `on_tie allow` | refuses `G-MODEL-PLACE` | `BAD\|unexpected token at top level` |

The gate refuses both, so it never admits a program whose aggregation it cannot
decide. It errs in the false-reject direction, which is the direction the
census docstring names as the one the crate is allowed to err in.

The marker is the generic top-level parse refusal rather than a named one, for
exactly the reason item 512's section 7 records: that is the state of every
contextual top-level declaration the reference has added since the self-host's
top-level dispatch was written, and the same gate answers
`BAD|unexpected token at top level` for a shipped `retention` declaration. A
named marker is item 512's slice 3, and a `MODEL` marker covering both shapes
`model` heads would serve this item too.

Because no `.rvl` in any corpus directory uses the construct, the census is
unmoved: `--check` reports no change from the baseline, `false-reject` is still
empty.

### 13.1 Why the fixtures are inline

The test programs live as strings in `tests/test_model_council_516.py` rather
than in `examples/rejections/` or `tests/fixtures/`. Both are corpus roots for
`tools/gate_reference_census.py`, and the self-host does not parse
`model council`, so an ADMITTING fixture in either place would have become a
`false-reject` census entry the moment it landed. This is item 512 section 6.1's
decision, held for the same reason. A fixture belongs there when the self-host
port lands, and moving it is then part of that slice's evidence.

---

## 14. Slice plan

Each slice is independently landable and carries its oracle in the same PR.
`tests/test_model_council_516.py` is the standing guard on every one.

**S1. The declaration, checked. LANDED with this note.** The surface of section
2, the thirteen refusals of section 8, no IR, no new guarantee code.

**S2. Binding a council to an action.** A `route model` arm names a council
where it today names a role, and the council's residence (section 6) is what
item 514's ceiling reads, so a confidential origin routed to a council with one
`off_device` member is refused naming **that member**. This is the slice that
makes the separate placement bite on a value. It reads
`revl.model_council.check()`'s table and `revl.model_route.check()`'s route
table and re-derives neither.

**S3. The answer type.** `Answer[T]` and `Aggregate[T]` of section 3 as real
types over the item-257 boundary, and the exhaustiveness rule: a match on an
`Aggregate[T]` that omits `Split` or `Inquorate` is refused. This is the slice
that completes the roadmap's first exit clause, "a two-member council that
disagrees does not admit", at the call site rather than in the declaration.

**S4. Per-member inputs.** The declaration that gives the adversary an input the
proposer is not given, which is what makes section 6.1's sentence literally
true. Needs the origin ceiling per member from S2 and is the reason this is not
folded into it.

**S5. The self-host port.** A named `MODEL` marker in `selfhost/parser.rvl`
covering both shapes `model` heads, then the port. Both touch the crate closure,
so both regenerate `crates/**` with `build_gate_crate.py` AND
`build_gate_wasm.py`, and both must run `tests/test_gate_crate_admit.py`,
because the two drift gates compare bytes only and a byte-correct regen has
failed `cargo` before.
