# 539: The model portfolio as a device-profiled, schedulable placement

Roadmap: item 515 (issue #1189), from the 2026-09-19 external review. Slice 1
is LANDED with this note; slices 2 to 5 are designed here and not written.

Number 539 was taken because 531 to 538 are claimed: 531 merged (PR #1220,
extended by #1243), 532 by PR #1228 and #1242, 533 by #1232, 534 by #1231,
535 by #1230, 536 by #1239, 537 by #1241, 538 by #1242. The claim was checked
against the diffs of every open pull request, not against `main`, because four
lanes collided on 531 in one day.

Sits inside: item 512 and `docs/design/531-model-placement.md`, whose section 9
says of this item that it "schedules INSIDE the boundary; owns the device
profile; must not be able to pick a role no arm names". That last clause is a
refusal, and slice 1 makes it one.

Reconciles with: item 514 and 531's section 3.2 (the origin ceiling this must
not be able to move work past), item 517 and
`docs/design/536-model-decision-evidence.md` (whose one open ask, "publish what
`placement_digest` is computed over", section 5 below answers), item 538 and
upstream `inso1337/revl-harness#10` (the provider side, which owns the
published profile), item 411 and `docs/design/411-sandbox-placement.md` (a
declared placement claim, checked against arms and not against a machine),
item 243 and `docs/design/teardown-contract.md` (the lifetime machinery a
shared provision will reuse).

---

## 0. The decision in one paragraph

A model is not placed like an ordinary provider. It has a device profile, a
load and unload cost, and a lifetime that should be shared, and revl's
placement reasons about realms and hosts, so a member that needs a GPU is
indistinguishable from one that needs a CPU. This note adds the two facts a
scheduler cannot be written without and refuses the two ways a scheduler undoes
an admission decision. A **`model role`** may carry a **device profile** -
`device <class> memory <MiB> quant <tag>` - which is the resource the placement
DEMANDS. A **`route model` arm** may name an **ordered candidate set** -
`confidential -> fast | small` - which is the scheduling surface. The set is
CLOSED: `*` on the right of an arrow is refused by name, residence is uniform
across a set, and every candidate in a set is profiled. The scheduler itself is
not in this slice; what is in it is the shape that makes a scheduler safe to
write later.

---

## 1. The gap

Three sentences from the issue, and what each one costs today.

**"A model that needs a GPU is not distinguishable from one that needs a
CPU."** A role is a name and a residence (item 512). Nothing in a revl document
states a resource requirement, so admission cannot refuse a placement the
target cannot satisfy, and a `route model` arm choosing between two roles is
choosing between two names.

**"Two components cannot share one loaded model by key."** A model reached
through an extern is loaded per call site. The provision machinery that already
gives one key one instance with one teardown (item 243's witness pair, the
G5/G7 contract) never sees a model, because a model is not provisioned.

**"The small model is resident here is not expressible."** Residency is a fact
about a placement over time. There is no placement object to attach it to.

This item matters disproportionately for a local-first deployment, where the
portfolio is several small members on one machine rather than one API
endpoint. With one endpoint, the device profile is the vendor's problem; with
six members and one GPU, it is the program's.

---

## 2. The surface

Both additions are OPTIONAL and both are contextual identifiers, so the
lexer's `KEYWORDS` table and the self-hosted lexer that mirrors it need no
sync, and no program that compiles today stops compiling.

```revl
model role fast  on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory  512 quant int8
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> fast | small,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
```

**`device <class> memory <MiB> quant <tag>`** is an optional clause after a
role's residence. `class` is a closed vocabulary, `cpu`, `gpu` or `npu`.
`memory` is the resident memory the placement needs, in MiB, a positive
integer. `quant` is an opaque quantisation tag. `device`, `memory` and `quant`
are read only in this slot, immediately after a residence, so a program using
any of the three as an ordinary name keeps parsing.

**`<origin> -> <role> | <role> | ...`** is an ordered candidate set. The head
is the placement item 514's ceiling reads, and is exactly what the arm meant
before this surface existed. The tail is what a scheduler may fall back to,
in the order written.

### 2.1 The slot after the residence is shared

Item 519 (`docs/design/541-model-in-attenuation.md`) adds a
`reaches [...]` clause in the same position, so a role may carry two
optional clauses after its residence:

```revl
model role fast on_device device gpu memory 6144 quant q4_k_m
                          reaches [model.complete]
```

The order is fixed, `device` first and `reaches` second, and each clause is
independently omittable: a role may write neither, either or both. The other
order is a refusal rather than a second accepted spelling, because one
declaration with two spellings makes every later reader of this slot carry the
permutation.

The order follows the reading. `device` refines the residence in front of it,
since both answer where the call runs, so the placement facts stay together.
The bracketed capability list reads last, in the position `requires` and
`emission` have already taught a reader to expect one.

The two clauses also fail in opposite directions, which is why neither stands
in for the other. An omitted `device` clause is a role making no resource
claim, refused only where a claim is needed, which is an ordered candidate
set. An omitted `reaches` clause is a reach nobody wrote down, refused
wherever it is read.

`ModelRoleDecl` and `model_route.Role` carry both clauses as keyword-defaulted
fields, so neither owns a positional slot and `Role(name, residence, line)` is
still the whole declaration for a role that writes neither. Item 516 reads a
role through `model_route.roles()` and is unaffected by either.

The two items meet once more past the grammar: the item-519 reach fold runs
over every candidate of an item-515 ordered set and not only the head, because
a fallback the scheduler may pick is a role the component routes through, and
a fold that read only the head would let the first fallback widen a ceiling
the head respects. `tests/test_model_role_clauses.py` is the executable spec
for the whole shared slot.

### 2.2 Demand and supply are two different objects

This is the reading the rest of the note depends on, and it is the seam item
538 drew.

The `device` clause is the **demand**: what the program says this placement
needs. It is written in a revl document, checked by the compiler, and visible
to a reviewer.

The **supply** is what the member was actually loaded onto: the device, the
resident memory, the quantisation, the runtime build, the weights. It is a
property of a host. Item 538 records the decision that a device profile, a
load cost and a quantisation "are properties of a host and nothing under the
compiler should learn what one is", and that the provider publishes the profile
rather than configuration asserting it. The supply reaches revl only as an
opaque digest (section 5).

The compiler compares the demand against the arms that name the role and
against the ceiling above it. **It never compares the demand against the
supply, and it cannot.** Section 9 says so again, because that is the sentence
a reader is most likely to assume the opposite of.

---

## 3. What the checker decides

The device-profile rules live in `src/revl/model_route.py` beside item 512's,
because both are rules over the same declaration. The digest definition lives
in `src/revl/model_profile.py` because nothing in the compiler calls it.

| # | The decision | Refusal cites | Direction |
| - | ------------ | ------------- | --------- |
| 1 | a device class is in the closed vocabulary | `G-MODEL-PLACE` | closed: `device gpu0` is a refusal, never a profile that matches any device |
| 2 | a memory floor is positive | `G-MODEL-PLACE` | closed: `memory 0` reads as a declared requirement and rules nothing out |
| 3 | a `device` clause carries all three parts | parse | closed: a half-written profile is not a profile |
| 4 | `*` is not a candidate, in any position | `G-MODEL-PLACE` | closed: this is 531 section 9's refusal, by name |
| 5 | a candidate appears at most once in a set | `G-MODEL-PLACE` | closed: a repeated name has two positions and no defined preference |
| 6 | every candidate is a declared role | `G-MODEL-PLACE` | closed: item 512's rule, extended from the head to the tail |
| 7 | no candidate of a confidentiality origin is `off_device` | `G-MODEL-PLACE` | closed: item 512's rule, extended from the head to the tail |
| 8 | the `secret` origin reaches no candidate | `G-SECRET-FLOW` | closed: item 512's rule, extended from the head to the tail |
| 9 | residence is uniform across a candidate set | `G-MODEL-PLACE` | closed: a scheduler may not choose the residence |
| 10 | every candidate of a multi-candidate set declares a device profile | `G-MODEL-PLACE` | closed: an unrankable candidate is what a fallback lands on |

Decisions 6, 7 and 8 are not new rules. They are item 512's rules reaching the
tail of an arm, and they are listed because until this slice the tail did not
exist and the head was the whole arm. An extension that had left them on the
head would have been the item's own failure mode: a rule keyed to a placement
that outlived the placement meant to bound it.

### 3.1 The two that are this item's own

**Decision 9, residence is uniform.** Item 514's ceiling refuses a value whose
origin reaches an `off_device` role, and it reads one residence per origin. An
arm whose head is `on_device` and whose fallback is `off_device` lets a
scheduler carry a workload across the line the ceiling drew, at a moment no
admission check is watching. That is the fail-open shape in its scheduling
form, and the brief's own statement of the direction applies to it literally: a
scheduler that falls back to any available device when the declared profile is
unavailable is exactly how a confidential workload ends up on the wrong
hardware.

```revl reject G-MODEL-PLACE
model role fast  on_device device gpu memory 6144 quant q4_k_m
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> fast | cloud
  }
  provide out { fn classify(text) = text }
}
```

    action `classify` (Classifier) routes the `confidential` origin to model
    role `cloud`, which is declared `off_device` on line 2: a confidential
    input may not leave the device (G-MODEL-PLACE)

On a non-confidential origin the ceiling has nothing to say, and decision 9
still refuses, because the question is not whether this particular value may
leave the device but whether a scheduler gets to decide:

```revl reject G-MODEL-PLACE
model role near on_device  device cpu memory 512 quant int8
model role far  off_device device cpu memory 512 quant int8

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    public -> near | far
  }
  provide out { fn classify(text) = text }
}
```

**Decision 10, every candidate is profiled.** An arm naming one role is a
placement. An arm naming several is a placement plus a scheduling decision, and
the declared device profile is what that decision is made on. A candidate with
no `device` clause is not comparable to one that has it, and an incomparable
candidate is precisely the one a fallback lands on when the profiled ones are
unavailable. Fail-closed means the whole set is comparable or the program does
not compile. A single-candidate arm needs no profile, which is why nothing
written against item 512 stops compiling.

### 3.2 The refusal section 9 asked for

531's section 9 says a scheduler "must not be able to pick a role no arm
names", and adds that item 512's slice 4 is what would make that refusable. It
is refusable earlier than that, in one specific and useful place: the spelling
an author reaches for. `confidential -> *` parses, and is refused by name
rather than by a syntax complaint, so the diagnostic gives the reason instead
of the grammar:

```revl reject G-MODEL-PLACE
model role fast on_device device gpu memory 6144 quant q4_k_m

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> *
  }
  provide out { fn classify(text) = text }
}
```

    `confidential -> *` in `route model on classify` (Classifier) places the
    origin on any available role (G-MODEL-PLACE)

This is narrower than 512's slice 4 and does not replace it. Slice 4 checks
which role a crossing DOES reach; this checks that the written set of roles it
MAY reach is enumerated. Both are needed, and this one is available now.

### 3.3 No new guarantee code

Every refusal above cites `G-MODEL-PLACE`, whose published text is already
"a model role declared `off_device` never receives a confidentiality origin,
an action reaches only the roles its `route model` block names, and a role
reaches no capability the component routing through it holds". The second
clause is decisions 4 to 10 verbatim; nothing here is a guarantee that text
does not make. (The third clause is item 519's, added to the same sentence
for the same reason.)

Registering a second code would also have meant a row in the generated tier
matrix of item 523, which needs a reproducer under `examples/rejections/` or an
`ACKNOWLEDGED` entry in `tools/tier_guarantees.py` or generation fails. The
reproducers for this slice are inline for the census reason in section 6.1, so
the entry would have been the acknowledgement route, in the same generated
table PR #1247 is currently repairing. Not registering a code is the right
answer on the merits and avoids that collision; had the merits gone the other
way the acknowledgement would have landed in this PR.

---

## 4. Why slice 1 is not the scheduler

The scheduler is the part that reads a candidate set, asks a provider which
members are resident, and picks. It needs three things that do not exist yet: a
provision keyed by role (slice 3), a published profile to compare the demand
against (item 538, upstream), and a load and unload cost model (slice 4).
Writing it before those exist would mean inventing all three inside it.

The mechanical consequence, which is item 512's property and is kept here: a
device profile and a candidate set contribute NOTHING to the IR.
`test_a_profiled_placement_writes_no_ir` pins it by compiling the flagship
program and the same program with every declaration deleted and comparing the
two IRs. No emitter changed, no golden moved, the manifest is unchanged.

The edge an author must not fall off is the same one 531 section 4 names, and
the same two things keep it from being a silent wrong answer. The declarations
grant no ability, so there is no behaviour to be surprised by; and every
decision in section 3 points at refusing, so the worst outcome available from
the declaration alone is a program that does not compile.

---

## 5. What `placement_digest` is computed over

Item 517's note ends with one open ask, addressed to this item's
neighbourhood: "publish what `placement_digest` is computed over." It is
answered here, and in `src/revl/model_profile.py`, which is the normative copy.

`placement_digest` is `sha256` of a preimage, rendered as 64 lowercase hex
characters. The preimage is UTF-8 and is built from a version line and seven
field lines:

    revl-placement-v1
    role=<the model role name this placement is bound to>
    residence=<on_device|off_device>
    device=<the device class the member was loaded onto>
    memory_mib=<the resident memory of that load, decimal>
    quantisation=<the provider's quantisation tag>
    runtime_build=<the inference runtime's build identifier>
    weights_digest=<a digest of the weights actually loaded>

Four encoding rules, stated so another tier reimplements them rather than
importing the module:

1. The preimage is UTF-8 bytes.
2. The version line comes first, then one line per field in the order above,
   none omitted.
3. A field line is `name=value`, and every line including the last ends with a
   single LF. A reader splits on the FIRST `=`, so a value may contain `=` and
   a name may not.
4. A value contains no LF and no CR. There is no escaping and no quoting: a
   value that would need one is refused rather than mangled, because a value
   carrying a line break could forge a field line.

**Why each field is in.** `role` and `residence` bind the digest to the
placement revl admitted, so a record claiming role `local` whose digest was
computed under `cloud` is detectable by a verifier holding the provider's
profile table. `device`, `memory_mib` and `quantisation` are the three the
issue names as the device profile, and they are what makes one model at three
quantisation and memory points three placements rather than three calls.
`runtime_build` is in because two runtime builds at one quantisation produce
different distributions, which is the same argument that put `placement_digest`
in the record at all. `weights_digest` is in because item 517's `model_digest`
names the model and not the file, and a fine-tune is a different placement.

**Which side computes it.** The provider. Five of the seven fields are facts
about a host that revl never learns, and item 538 is the reason. revl's
obligation, per item 517, ends at binding the value into the record's MAC.
`test_nothing_in_the_compiler_computes_a_placement_digest` pins that a compile
of the flagship program calls `placement_digest` zero times.

**What a refusal looks like here.** `placement_preimage` refuses a missing
field, an unknown field and a value containing a line break, and raises rather
than skipping. A digest over six of seven fields would collide two placements
that differ only in the seventh while still looking re-runnable, which is the
same shape as a MAC over a subset of a record.

**The declared side.** `declared_floor()` returns the three demand fields
(`device`, `memory_mib`, `quantisation`) as plain data, for a provider to
compare against its own published profile. Those three names are deliberately
the same as the supply's, so the comparison is field by field. revl does not
perform that comparison.

---

## 6. Where it lands

| File | Change |
| ---- | ------ |
| `src/revl/parser.py` | `DeviceProfileClause`; `ModelRoleDecl.profile`; `ModelRouteArm.alternates` and `.candidates`; the `device` clause and the `\|` candidate loop; `_model_route_candidate` |
| `src/revl/model_profile.py` | NEW. `DEVICE_CLASSES`, `DeviceProfile`, `declared_floor`, `PLACEMENT_DIGEST_FIELDS`, `placement_preimage`, `placement_digest` |
| `src/revl/model_route.py` | `Role.profile`, `_profile()`, `_check_candidate_set()`, the per-candidate loop in `check()`, `candidates` in the returned table |
| `tests/test_model_portfolio_515.py` | NEW. 30 tests |

No emitter, no manifest, no IR, no lexer, no crate. `tools/build_gate_crate.py
--check` reports in sync, which is the authoritative answer and not an
inference from the file list. No guarantee registry row moves (section 3.3), so
`docs/rejections.md`, `docs/guarantees.md` and `docs/DOC-STATUS.md` are
untouched.

### 6.1 Why the fixtures are inline

The same reason 531 section 6.1 gives, unchanged: `examples/rejections/` and
`tests/fixtures/` are corpus roots for `tools/gate_reference_census.py`, and
`selfhost/parser.rvl` does not parse `route model`, so an admitting fixture in
either would become a `false-reject` census entry the moment it landed.
`false-reject` is empty and this slice keeps it empty. A fixture belongs there
when 512's slice 3 lands the self-host port, and moving it is part of that
slice's evidence.

### 6.2 Compatibility with the route table item 514 reads

`model_route.check()` returned `{component: {action: {origin: {role,
residence}}}}` and now returns the same dict with a `candidates` key added.
`role` and `residence` are the arm's HEAD and are byte-identical to what they
were, so item 514's flow walk (PR #1243) reads exactly what it read before and
needs no change to land on top of this. An arm written with a single role
reports a one-tuple, not an absent key, so a consumer never has to distinguish
"no candidates" from "one candidate".

---

## 7. The self-host question

`selfhost/*.rvl` is a second implementation whose oracles catch divergence but
not a missing feature, so the question is whether the gate can SILENTLY ADMIT a
program the reference decides.

It cannot, for the same reason 531 section 7 measured: the self-host's
top-level dispatch does not parse `model role` at all, so it answers
`BAD|unexpected token at top level` to every program in this note, including
the admitted ones. It errs in the false-reject direction, which is the
direction the census docstring names as the one the crate is allowed to err in.
Nothing in this slice narrows or widens that: the `device` clause and the `|`
separator sit inside a construct the gate already declines whole.

Because no `.rvl` in any corpus directory uses the construct, the census is
unmoved. The named `MODEL` marker that would make the gate say WHICH construct
it declines is the first half of 512's slice 3 and is unchanged by this note.

---

## 8. Slice plan

Each slice is independently landable and carries its oracle in the same PR.
`tests/test_model_portfolio_515.py` is the standing guard on every one.

**S1. The declaration, checked. LANDED with this note.** The device profile of
section 2, the candidate set, the ten decisions of section 3, the
`placement_digest` definition of section 5, no IR.

**S2. The provision, keyed by role.** A `model role` becomes bindable to a
provision key, so two components injecting one role share one loaded member
rather than loading twice. This is the issue's second sentence and it is where
the existing lifetime machinery does the work: item 243's witness pair for
acquire and release, the G5/G7 teardown contract for unloading once, the
handoff rule for a member passed between components. The exit evidence the
issue names belongs to this slice, not to S1: a teardown that unloads once for
N consumers with `no_residue`.

**S3. Residency, and the load cost.** "The small model is resident here" is a
statement about a placement over time. It needs a load and unload cost on the
profile and a residency claim the scheduler can read. Both are provider facts
(item 538), so the slice is mostly a seam: what the program declares is the
demand, what the provider publishes is the residency, and the scheduler is the
only thing that sees both.

**S4. The scheduler.** Reads a candidate set and picks. It becomes writable
once S2 and S3 exist, and section 4 says why not before. The refusals of S1 are
what make it safe: the set is closed, the residence is uniform, and every
candidate is comparable, so the scheduler chooses within a placement rather
than about one.

**S5. The role in the manifest.** 531's own S5, which this item needs: a role
declared in the composition manifest and bound to a member by configuration,
which is what item 538 means by "a role is declared once and bound to a member
by configuration". S2's provision key is the natural place that binding lands.

---

## 9. Things stated here that are not verified

* **That a declared device profile corresponds to any hardware.** This is the
  most important line in the note. `device gpu memory 6144` is a claim written
  in a program. The compiler checks it against the closed vocabulary, against
  the other candidates in its arm, and against the residence rules above it. It
  does not check that a GPU exists, that 6144 MiB are free, or that the member
  bound to the role was built for that device. It has no way to: those are
  facts about a machine at a moment, and the compiler reads a file. The
  comparison between the declared demand and the published supply is a runtime
  check a provider performs, and item 538 owns it. A reader who takes an
  admitted program as evidence that the hardware is there has read this surface
  wrongly, and this paragraph is the correction.
* **That `on_device` corresponds to any physical fact.** Unchanged from 531
  section 10; the device profile does not make it more true.
* **That the ten decisions are complete.** They are the ones a declaration and
  a candidate set can be wrong in. The value side is item 514's and has landed
  on its own branch; the crossing side is 512's slice 4 and is open; the
  scheduler's own decisions are S4's and do not exist.
* **Anything about tiers other than the reference.** Nothing in this slice
  reaches an emitter, so there is nothing tier-specific to be right or wrong
  about, but that is an argument from the file list and not a measurement. The
  measurement that IS available is the IR comparison in section 4.
* **The self-host numbers in section 7** are inherited from 531's measurement
  of the same gate on the same construct, not re-measured here. What was
  re-measured is that no corpus `.rvl` uses the construct, which is what keeps
  the census unmoved.
