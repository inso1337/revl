# 549: The AIOS kernel is a consumer of revl, and where the line between them runs

Roadmap: item 540 (issue #1209), from the 2026-09-19 external review. This note
is the boundary statement item 540 asks for. It adds no language surface, no
module under `src/revl/`, and no test of a kernel, because there is no kernel.
What it adds is the answer to the two questions item 540 says are unanswerable
from the tree: what does such a system take from revl, and what is it allowed
to assume.

The design-doc number was **assigned**, not scanned for. Four lanes collided on
531 in one day by each taking the next free number against the same `main`, and
537 and 538 each record a further collision in the same window. The roadmap item
of the same number is unrelated, which is the convention 457 established.

Sources read at `a80e8a93` unless stated: `src/revl/model_route.py`,
`src/revl/ui_family.py`, `src/revl/policy.py`, `src/revl/attest.py`,
`src/revl/deploy.py`, `src/revl/peer_authority.py`, `src/revl/peer_offer.py`,
`src/revl/lawful_retry.py`, `src/revl/session_commit.py`,
`src/revl/model_evidence.py`, `tools/evolution_controller.py`,
`docs/design/531-model-placement.md`, `docs/design/532-typed-computer-use.md`,
`docs/design/536-model-decision-evidence.md`,
`docs/design/537-evolution-controller.md`,
`docs/design/538-ui-transactions.md`,
`docs/design/480-verifiable-private-peer-pool.md`,
`docs/design/338-revl-as-dependency.md`,
`docs/design/469-enforced-policy-drift-control.md`,
`docs/design/teardown-contract.md`, `docs/guarantees.md`,
`docs/capability-attenuation.md`. Four unlanded branches were read with
`git show`: item 515 on `agent/1189-model-portfolio` (PR #1252), item 519 on
`agent/1193-model-in-attenuation` (PR #1253), item 516 on
`agent/1190-model-council` (PR #1257) and item 544 on
`agent/1223-kernel-boundary-capability` (PR #1264). Section 10 says which
claims rest on a branch rather than on `main`.

---

## 0. The decision in one paragraph

The AIOS kernel is a **consumer** of revl with a declared dependency, which is
the surface `docs/design/338-revl-as-dependency.md` publishes, and not a
stratum of the language. The line is drawn per capability rather than per
component, and it has one shape everywhere it is drawn: **revl decides whether
an action is permitted and what evidence it leaves; the kernel decides which
resource performs it and holds every fact about a machine at a moment.** Five
capabilities are adjudicated below against the item that owns each side. Two of
them, peer elasticity and the composition's own teardown at portfolio scale,
have a revl side that is landed and a consumer side that has no owner. The half
of this note that is load-bearing is section 5, because the failure mode item
540 names is a consumer that assumes a guarantee revl does not make, and every
assumption in section 4 is paired there with the sentence that bounds it.

---

## 1. There are exactly two ways to get this wrong, and they cost differently

**Treating the kernel as a revl feature.** Devices, memory, peers, scheduling
and UI are host concerns whose vocabulary changes per operating system and per
toolkit. A language that learns them acquires a surface it must keep compiling
on seven tiers forever. Item 521 already settled the shape of the refusal for
one of them: a computer-use verb is an ordinary capability token in a reserved
namespace precisely because "the effect surface is OS- and toolkit-specific,
but the *authority* surface is not"
(`docs/design/532-typed-computer-use.md`, §0). The same reasoning applies to a
scheduler and to a peer transport, and item 538's decision that a device
profile, a load cost and a quantisation "are properties of a host and nothing
under the compiler should learn what one is" is the same sentence a third time.

**Treating the kernel as unrelated.** The review's strongest claim is that the
pieces already add up. If the composition is nobody's subject then item 520's
invariant, that a system may evolve its behaviour and never the rules that
govern its authority, is a property of a lifecycle with no running subject, and
item 525's demonstration proves integration for the paths it exercises without
stating what any other consumer may assume. That is the status quo item 540 was
filed against.

The asymmetry worth naming: the first mistake is expensive and visible, because
it shows up as code under `src/revl/` that a reviewer can point at. The second
is cheap and invisible, because it shows up as nothing. This note is written
against the second.

---

## 2. The line, in one table

```
AIOS kernel   devices, memory, peers, scheduling, UI
revl          authority, effects, admission, recovery
```

That is the review's own split and it is correct as far as it goes. It is not
yet operational, because both halves name nouns and the interesting cases are
verbs. The operational form, which the rest of this note applies:

| the question | who answers it | why |
| ------------ | -------------- | --- |
| may this action happen at all | revl, at admission | it is a property of a declared composition, computed from the source |
| which resource performs it | the kernel | it is a fact about a machine at a moment, and the compiler reads a file |
| what does it leave behind | revl, as a record | the WAL, the residue report and the attestation are revl's artifacts |
| did the outside world actually change | neither, today | revl proves the residue of what it held; see §3.5 |

The third row is why "authority, effects, admission, recovery" is not four
independent things. Admission decides, the effect records, and recovery reads
the record. A consumer that takes the effect path and not the admission path
gets a log rather than a guarantee.

---

## 3. The five capabilities the review's sentence implies

The review's sentence is: *revl becomes the operating system for a portfolio of
specialized local models, where each model makes only the decisions it is
suited for and every consequential action passes through a typed,
capability-checked runtime.* Five capabilities have to exist for it to be true.
Each is adjudicated here with the item that owns each side.

### 3.1 The model portfolio

**revl's side, and the item that owns it.** A placement is a checked
permission. Item 512 (`docs/design/531-model-placement.md`,
`src/revl/model_route.py`) makes a `route model on <action>` block decide which
declared role an origin class may reach, refusing under `G-MODEL-PLACE`, whose
published text in `src/revl/diagnostics.py` is "a model role declared
`off_device` never receives a confidentiality origin, and an action reaches
only the roles its `route model` block names". Item 514 adds the origin
ceiling. Item 516 (PR #1257) adds the council and the rule that disagreement
can never be silently resolved toward allow. Item 519 (PR #1253) puts the model
role in the attenuation product so the effective ceiling is an intersection
rather than what the component alone holds.

**The kernel's side.** Which member answers a given call, when a member is
loaded and unloaded, and what hardware exists. Item 515 (PR #1252) draws that
seam explicitly and names the two objects: the `device` clause is the **demand**
written in a program, and the supply is what the member was loaded onto, which
is a property of a host. Its §2.1 states that the compiler "never compares the
demand against the supply, and it cannot".

**What nothing in this repository owns.** The provider side, by decision rather
than by gap: it is filed upstream as item 538 and
`inso1337/revl-harness#10`. The scheduler is item 515's slice 4 and does not
exist; §4 of that note says why it is not writable before the provision and the
published profile exist.

### 3.2 Typed action

**revl's side.** Item 521 makes a computer-use verb a capability token in a
reserved, closed namespace (`src/revl/ui_family.py`), so every authority surface
that already reads a capability token reads a UI verb without being taught
anything: the G4 subset check, the G8 audit reach, `secret K for C`, the
item-246 approval gate and item 249's derived sink and source classes, which
read the token's dotted head. Item 522 makes the reversibility class
registry-owned rather than author-declared, with `unknown` handled exactly as
`irreversible` (`REVERSIBLE`, `COMPENSATABLE`, `CONFIRM_REQUIRED`,
`IRREVERSIBLE`, `UNKNOWN` in `src/revl/ui_family.py`).

**The kernel's side.** The substrate that performs the click, and the order of
the fallback ladder. Item 539 files both upstream as
`inso1337/revl-harness#11`. Item 521 keeps the *admissible depth* of the ladder
on revl's side by making a rung part of the capability token, so descending the
ladder crosses a different declared boundary; the order in which a substrate
tries the rungs is not revl's.

**What nothing owns, and it is inside revl rather than outside it.** The rule
the review's sentence most depends on, that a screenshot or a window title is
attacker-influenced input which may not become an action, is **not enforced
today**. `docs/design/532-typed-computer-use.md` §12 records that `screen` and
`ui` are not in `_SOURCE_CLASS_SCOPES` or `_SINK_CLASS_SCOPES`, "so nothing yet
refuses an observed string flowing into a click", and
`docs/design/538-ui-transactions.md` §11 records that slice 1 "does not stop a
click. It stops a program from claiming a click is clean." Both are inside
items 521 and 522 as later slices. A consumer reading the review's sentence and
not those two paragraphs would assume the wrong thing today.

### 3.3 Peer elasticity

**revl's side.** Three primitives are landed and are language-level kernels:
the authority-monotonicity invariant (`src/revl/peer_authority.py`), under which
a peer receives no more authority, data, time, money or retry budget than the
delegating composition holds; the signed peer offer with attestation and
placement matching (`src/revl/peer_offer.py`); and the lawful-retry dispatcher
(`src/revl/lawful_retry.py`). `docs/design/480-verifiable-private-peer-pool.md`
states their status and their limit in the same paragraph: the primitives are
proven in isolation, and wiring them to a live dispatch "is NOT a language
primitive".

**The kernel's side.** Transport, discovery, membership, health, the delivery
ledger and the operator authority view. Item 524 (issue #1198) names the CLI and
the rules it must state, including one-result delivery with a ledger, liveness
and withdrawal as normal events, and per-peer attenuation reusing the existing
product rather than a parallel ACL. It is unstaffed. Item 524 also draws this
note's split for itself, in the same words item 512 uses for a model route: a
recommendation is not an admission, and the machine that recommends a peer must
not be the machine that admits it.

**What nothing owns.** The live dispatch between the two halves. `480`'s own
statement is that it needs item 421's F8 network seam and item 107's seam
transport. Until then, peer elasticity is a capability the review's sentence
asserts and the tree supplies only the authority algebra for.

### 3.4 Evidence

**revl's side.** Item 517 landed the sealed `ModelDecision` record
(`src/revl/model_evidence.py`), binding the crossing, the placement, the model,
the prompt binding, the candidate set, the request parameters, the policy in
force by digest and the fallback depth, with a fail-closed verifier. Item 127
and item 469's slice bind the composition IR hash, the checker identity and the
ruleset digest into a signed attestation (`src/revl/attest.py`), and
`src/revl/deploy.py`'s `chain_bindings` folds the per-facet sha256 of the
artifact bytes, the capability policy, the component lock, the gauntlet evidence
and the per-backend conformance cert into that signature, so one signature
commits source to IR to artifact to policy to evidence.

**The kernel's side.** Every fact about a host that the record names and revl
does not compute. Item 515's §5 is the exact case: `placement_digest` is
computed by the **provider**, over the role, the residence, the device, the
resident memory, the quantisation tag, the inference runtime's build identifier
and a digest of the weights actually loaded. Five of those seven are facts about
a machine revl never learns, and revl's obligation ends at binding the value
into the record's MAC.

**What nothing owns.** Two things. Nothing writes a `ModelDecision` during a
run yet; `docs/design/536-model-decision-evidence.md` §9 says so in its own
words, and the "a recorded run replays a model decision from the artifact alone"
half is slices 2 and 3. And there is no evidence object for the **kernel's own**
steps, as distinct from the model's choice: a device grant, a peer admission and
a UI transaction each have a decision worth recording and none has a record.
Item 522 makes that concrete in the negative: "no `uncompensated` value is
produced anywhere".

### 3.5 Rollback

**revl's side.** G4, every mutation carries an inverse or admits irreversibility
with `emit`, enforced in the emission fixed point in `src/revl/lower.py`. G7,
derived LIFO teardown. Item 247's compensation entry kind and item 243's
transactional entry kind, combined into one loop with one residue schema in
`docs/design/teardown-contract.md`. The residue report
(`src/revl/erase_report.py`) and its `no_residue` verdict.

**The kernel's side.** Whether the outside world returned to an earlier state.
revl proves the residue of what **it** held. A GUI's state is not held by revl,
and that is the reason item 522 takes the reversibility class out of the
author's hands: the only thing revl can honestly own about a target whose state
it does not hold is whether a claim about that state is admissible.

**What nothing owns.** Item 540's own exit clause: a provider withdrawn
mid-flight leaving a teardown whose residue is **proved rather than asserted**,
at portfolio scale rather than at test scale. The teardown contract is a
per-activation loop and the deploy note is blunt about the limit that matters
here: the LIFO rollback theorem `apply` proves is an in-process property and it
does not cross a seam, because a peer process's inverses live in that process's
memory and its own durable WAL. A composition spanning a model portfolio, a peer
pool and a UI substrate is a composition whose inverses are in several processes
at once. Nothing measures that today.

### 3.6 The gaps, collected

Stated once, in a list, because a gap spread across five subsections reads as an
aside:

1. **Observed UI input is not taint-classed.** Inside items 521 and 522, later
   slices, not filed separately.
2. **A UI crossing with no inverse is still admitted with no confirmation.**
   `docs/design/538-ui-transactions.md` §6 and §11; deliberate, and named there
   as the largest gap in that item today.
3. **Nothing writes a model decision during a run.** Item 517 slices 2 and 3.
4. **There is no live peer dispatch.** Item 524, plus items 421 F8 and 107.
5. **There is no evidence object for the kernel's own decisions.** No item.
6. **No teardown is measured at portfolio scale, across processes.** Item 540's
   own exit; no mechanism proposed anywhere in the tree.

Items 5 and 6 are the two this note found with no owner at all. Section 9 says
what to do about them.

---

## 4. What the kernel may assume

Seven assumptions, each one a property some mechanism in the tree actually
computes. Section 5 bounds every one of them and is not optional reading.

**A1. The declared boundary is enumerable.** G8. `revl audit` lists what a
component reaches, and a reach of `*` never satisfies a closed allow-list.

**A2. A capability is granted, never held.** The attenuation product
(`docs/capability-attenuation.md`, item 66) and the spawn-attenuation fold in
`src/revl/lower.py` over `cap_order.covers` are one definition of narrowing, not
two. A component cannot mint a capability for itself, and the kernel is a
component like any other.

**A3. A model placement is a permission checked at admission,** not a hint.
Item 512. The kernel picks a member inside the permitted set; it does not get to
skip the check.

**A4. A UI verb is an ordinary capability token.** Item 521. The kernel does not
get a private write path to the screen any more than it gets one to a database.

**A5. Every mutation carries an inverse or says out loud that it does not.** G4.
An irreversible step is spelled, not discovered.

**A6. A delegation narrows.** `src/revl/peer_authority.py`. Authority may narrow
along a delegation edge and along a retry reissue, never widen.

**A7. A model decision can be sealed and verified fail-closed.** Item 517. Every
member is required, every vocabulary is closed, and a verifier that has not run
every check does not return a pass.

---

## 5. What the kernel must provide itself

This is the section item 540 is really about. The template is item 515's
sentence about a device profile, which is the clearest example in this
repository of a boundary stated so a reader cannot over-read it:

> `device gpu memory 6144` is a claim written in a program. The compiler checks
> it against the closed vocabulary, against the other candidates in its arm, and
> against the residence rules above it. It does not check that a GPU exists,
> that 6144 MiB are free, or that the member bound to the role was built for
> that device. It has no way to: those are facts about a machine at a moment,
> and the compiler reads a file.

Eight things the kernel supplies, each paired with the assumption above that it
bounds.

**P1. The policy file, and therefore every approval requirement.** Bounds A4.
`capability C requires approval [ttl D] [require N of {a, b, c}]` is a rule in
an operator's policy file evaluated against the audit graph at admission
(`src/revl/policy.py`). revl enforces the rule the operator wrote. It does not
supply one. A kernel that writes no approval rule gets no approval pause, and
the review's phrasing that the pause is "a property of the composition rather
than of the kernel's diligence" is true only once the composition includes a
policy that says so.

**P2. The expected ruleset digest, held out of band.** Bounds A1, A2 and A7.
`src/revl/attest.py` states its own limit: an attestation says the frontend
identified by `checker` ran over this exact composition and admitted it, and "a
verifier that does not know the signer's `checker.ruleset` digest learns which
checker asserted it, not that the checker is any good."

**P3. Key management, rotation and distribution.** Bounds A7.
`docs/design/536-model-decision-evidence.md` §9: the threat model is a
shared-secret one, a holder of the signing key can seal any well-formed record,
and the verifier's body checks bound well-formedness rather than truthfulness.
The attestation construction is HMAC-SHA256 and the envelope's `sign_alg` today
accepts one value.

**P4. Every fact about a host, and the digest that summarises them.** Bounds A3.
Per item 515, the provider computes `placement_digest`; revl binds it.

**P5. The comparison between the declared demand and the published supply.**
Bounds A3, and it is a separate line from P4 on purpose. Item 515 exposes the
three demand fields as plain data for a provider to compare against its own
published profile, and states that revl does not perform the comparison. If
nobody performs it, an admitted program is a program whose declared floor was
never met.

**P6. The substrate that performs a UI action, and the ladder's order.** Bounds
A4. Item 539, upstream. Also, and this is the part a kernel author will trip
on, the G8 audit "does not and cannot verify that a host body reaches only the
boundary its declaration names"
(`docs/design/532-typed-computer-use.md` §12). A1 is a guarantee about
declarations, not about syscalls.

**P7. Peer transport, discovery, membership, health and the delivery ledger.**
Bounds A6. The monotonicity invariant is an algebra over a chain the kernel
constructs. It says nothing about a peer that never reports.

**P8. The scheduler, and the residency claim it reads.** Bounds A3. Item 515
slices 3 and 4.

One sentence covers what is left over. **revl's guarantees are about an admitted
composition, not about a running machine.** Every entry above is an instance of
that, and a consumer that wants a guarantee about a running machine has to
build the measurement itself and feed it back in as a declared fact, at which
point revl will check the declaration and not the machine.

---

## 6. The seven surfaces the kernel may not evolve, and what refuses each today

Item 520's invariant is that a system may evolve its behaviour and never the
rules that govern its authority. The review lists seven evolvable surfaces
(policies, routing, components, workflows, memory, runtime, device placement)
and seven that are the boundary itself. The test item 540 sets is not that each
of the seven is discouraged but that each **is refused by a named mechanism**.
Measured against the tree at `a80e8a93`, that test does not pass yet, and the
table says exactly where.

Three kinds of mechanism appear, and the difference matters: a **refusal** stops
the change, a **fence** routes it to a human, and a **digest** lets a verifier
who already holds the expected value detect it afterwards.

| # | surface | mechanism today | kind |
| - | ------- | --------------- | ---- |
| 1 | granting a capability | `FORBIDDEN_GRANT` in `src/revl/gate.py` over `_DECIDER_SERVICES`, restated as `DECIDER_SERVICES` in `tools/evolution_controller.py`; the spawn-attenuation fold in `src/revl/lower.py` | refusal |
| 2 | disabling a taint check | `src/revl/taint.py` is in `KERNEL_PATHS`, so a candidate whose measured diff reaches it is `KERNEL_INTERSECTION`; `taint` is in `RULESET_MODULES`, so its bytes move `ruleset_digest()` | refusal on the diff, plus a digest |
| 3 | widening secret access | `G-SECRET-FLOW`, and the taint module above | digest, plus a refusal only where the change edits the fenced module |
| 4 | removing an approval requirement | the `policy` facet of `chain_bindings` in `src/revl/deploy.py`, and `policy_digest` inside item 517's record | digest only |
| 5 | changing rollback semantics | `lower` is in `RULESET_MODULES`, so a change to the G4 fixed point moves the ruleset digest | digest only |
| 6 | replacing the admission kernel | `src/revl/admission.py` and `src/revl/admit_profile.py` are in `KERNEL_PATHS` | refusal on the diff |
| 7 | altering what "committed" means | none found | none |

Four findings follow, and each is checkable from the table.

**Only surfaces 1, 2 and 6 are refused.** The other four rest on a digest, which
is detection by a verifier holding an expected value, which is P2's problem
handed to the consumer.

**Surface 4 is the weakest, and it is the one the review's sentence leans on
hardest.** `src/revl/policy.py` is in neither `KERNEL_PATHS` nor
`RULESET_MODULES`, and an operator's policy file is not in `KERNEL_PATHS`
either, so a proposal that removes a `requires approval` rule is refused by
nothing and moves the ruleset digest not at all. It moves the deploy chain's
`policy` facet, so a receiver re-verifying a bundle against a previously known
chain detects it, and it moves `policy_digest` in a model decision record. Both
are after the fact and both require a holder of the earlier value.

**Surface 7 has nothing.** `src/revl/session_commit.py` is in neither list, and
the runtime half of the commit protocol lives under `backends/`, which
`tools/evolution_controller.py` deliberately excludes because `src/`,
`selfhost/`, `backends/` and `crates/` are the subject of the loop and fencing
them would forbid the work the loop exists to produce. That exclusion is right.
It does mean that the definition of "committed" is currently on the subject side
of the fence.

**Item 544 (PR #1264) closes part of this and not all of it.** It turns the
enumeration into capability tokens the attenuation product refuses, which is the
structural answer, and it is right that a diff check "cannot refuse a candidate
that changes kernel behaviour through a path that touches no enumerated file".
Its token set covers admission, attestation, taint, retention, the gate crate,
the census and `formal`. It does not add a token for the approval policy or for
the commit protocol, so surfaces 4 and 7 remain where this table puts them after
it lands.

---

## 7. What revl must never learn

Stated as refusals so a later reviewer can check a diff against them rather than
against a mood.

* Nothing under `src/revl/` learns what a device, a peer, a scheduler or a
  window is. Item 521 and item 515 each record the reasoning; this note only
  collects it.
* Nothing under `src/revl/` performs the demand-against-supply comparison of P5.
  The moment the compiler compares a declared floor to a measured machine, the
  compiler's answer depends on the machine it ran on, and an admission verdict
  stops being a property of the composition.
* No kernel-specific capability token is added to the closed registries. A
  kernel's verbs are ordinary tokens or they are refused; that is item 521's
  whole argument and it generalises.
* The kernel does not get an exemption from admission on the grounds that it is
  the kernel. Its ambient reach is the union of every provider it hosts and
  every model it routes to, which is item 540's observation that this is the
  first composition whose ambient boundary is larger than its declared one, and
  therefore the case where an admission check reading only declared capabilities
  would be exactly wrong. Nothing in this note closes that; §3.6 item 5 is where
  it lands.

---

## 8. Where this lands

Nowhere in the compiler. This note adds one file and changes nothing else. No
`src/revl/` module, no emitter, no IR, no guarantee registry row, no crate
digest input, and no row in `docs/DOC-STATUS.md`, which indexes the top-level
documents and not `docs/design/`. `tools/build_gate_crate.py --check` was run
and reports in sync, which is the authoritative answer and not an inference
from the file list; `tools/docgen.py --check` reports 11 generated blocks
current and 4 coverage checks passing.

There are no `revl` fences in this note, deliberately. Every construct it would
demonstrate is already demonstrated in the note that owns it, and a duplicated
fence is a second copy of a surface that can drift from the first.

---

## 9. Does this need a roadmap item

No new item. **Item 540 already is it**, and it landed on `main` after issue
#1209 was filed: the issue's "`AIOS` occurs zero times in
`docs/v2.0-roadmap.md`" was true when written and the term now occurs once, in
item 540 itself. Filing a second item for the same subject would duplicate it.

Two amendments are worth making to item 540's text, and the orchestrator owns
that file. Draft text for both, to be used or discarded:

**Amendment A, the two capabilities with no owner.** Item 540's exit names the
seven-refusals test and the portfolio-scale teardown. It does not name the
kernel's own evidence object. Suggested addition to the exit clause:

> and the kernel's own consequential decisions, a device grant, a peer
> admission and a UI transaction, each carry a record of the same grade as item
> 517's model decision, because a composition whose model choices are evidenced
> and whose kernel choices are not has moved the unaccountable step rather than
> removed it.

**Amendment B, the two surfaces that no mechanism refuses.** Item 540 asserts
that the seven non-evolvable surfaces each have a named mechanism that refuses
them. Measured in §6 above, two do not. Suggested replacement for that clause:

> each with a named mechanism that refuses it, which is today true of three of
> the seven: removing an approval requirement and altering what "committed"
> means rest on a digest or on nothing, because `src/revl/policy.py` and
> `src/revl/session_commit.py` are in neither the kernel enumeration nor the
> ruleset digest, and item 544's capability set does not add a token for
> either.

---

## 10. Things stated here that are not verified

* **No composition was built and no kernel was run.** Every statement in this
  note is a statement about the tree and about published design notes. Section
  6's table in particular is the result of reading four lists
  (`KERNEL_PATHS`, `AUTHORITY_FENCE`, `RULESET_MODULES`, the deploy facet keys)
  and checking membership. It is not the result of proposing a candidate that
  removes an approval rule and watching what happens.
* **"None found" in row 7 is a search result, not a proof.** It means that
  `src/revl/session_commit.py` is absent from the enumerations named in that
  section, and that reading the deploy facet keys and the ruleset module list
  turned up nothing that covers the commit protocol. A mechanism could exist
  somewhere those four lists do not reach, in which case row 7 is wrong in the
  direction of understating the tree.
* **Four items are read from branches, not from `main`.** Items 515, 516, 519
  and 544 are PRs #1252, #1257, #1253 and #1264. Anything this note attributes
  to them is attributed to an unlanded branch and can change before it lands.
  PR #1266 reconciles the clause order of 515 and 519, so the surface syntax
  quoted from either may move.
* **Item 517's landed half is `src/revl/model_evidence.py`, and its unlanded
  half is most of the item.** The record exists and verifies; nothing writes
  one during a run. Every sentence here about evidence at runtime is about a
  designed slice.
* **The claim that A1 through A7 are the assumptions a kernel would make** is a
  judgement drawn from the review's sentence and item 540's own list, not a
  survey of any consumer. A real consumer will assume something this list does
  not contain, and the honest expectation is that section 5 grows.
* **Nothing here is measured on a tier other than the reference.** No emitter is
  touched, so there is nothing tier-specific to be right or wrong about, but
  that is an argument from the file list and not a measurement.
* **The guarantee texts are quoted from `docs/guarantees.md` and
  `src/revl/diagnostics.py`,** not re-derived from the checker. Whether the
  shipped checker enforces exactly what those strings say is item 469's
  question and not this note's.
