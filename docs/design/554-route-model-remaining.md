# 554: the rest of `route model`: the self-host port, and the crossing side

Roadmap item 512 (issue #1186), slices 3 and 4 of the plan in
`docs/design/531-model-placement.md` section 8. That note designs the whole
item and is the one to read first; this one records only what slices 3 and 4
decided, measured, and deliberately left alone.

Slice 1 (the declaration, checked) and slice 2 (item 514, the origin ceiling
on a value) are landed. What item 512 recorded as NOT done was three things:
the value side, which became item 514 and is landed; the crossing side, which
is slice 4 and is here; and the self-host, which answered the construct with a
generic parse refusal and is slice 3, also here.

Reconciles with: `docs/design/531-model-placement.md` (the design), item 249
and `docs/design/249-taint-provenance.md` (the origin lattice), item 343 (a
crossing's capability is its declared token, which is what slice 4 reads),
item 391 (the self-host frontier), `tools/gate_reference_census.py` (the
verdict census the port is measured in), and `docs/conformance.md` (the
guarantee x tier matrix, whose `revl` column slice 3 moves).

---

## 0. The two decisions in one paragraph each

**Slice 3.** `selfhost/lower.rvl` now DECIDES the declaration half of model
placement instead of refusing it at parse. It reads `model role` at top level
and `route model` in a component prelude, applies the ten rules
`src/revl/model_route.py` applies, and spells each refusal byte for byte as
the reference spells it under a new verdict tag, `MODEL`. `selfhost/checker.rvl`
steps over a `model role` declaration so a document that declares one is
checked at all rather than failing at its top-level parse. Both crates are
regenerated.

**Slice 4.** A model crossing whose declared capability is `model.<R>`, where
`R` is a declared `model role`, is PLACED on `R`. An action that wrote a
`route model` block reaches exactly the roles that block names, and a crossing
on any other role is refused under `G-MODEL-PLACE`. This is the sentence item
515 inherits: a scheduler may reorder, fall back and decline inside the
boundary, and may not run the action on a role the program never named.

---

## 1. Slice 3: what the gate refused before, and what it refuses now

Section 7 of the first note measured the gap and named the direction it erred
in. Re-measured on `origin/main` at `dfecba2a`, before this branch:

| program | reference | gate |
| ------- | --------- | ---- |
| a component with no placement (control) | admits | `''` (admits) |
| `route model on classify { confidential -> local, * -> cloud }` | admits | `BAD\|unexpected token at top level` |
| `route model on classify { confidential -> cloud }` | refuses `G-MODEL-PLACE` | `BAD\|unexpected token at top level` |

The gate never admitted a placement it could not decide, so the shape was a
false REJECTION and not a bypass. It was still two things worth removing. The
marker was generic, so the refusal did not say which construct the gate
declined, so a consumer reading the wire learned only that something at top
level did not parse. And because the refusal was generic, an ADMITTING program
could not be put in `examples/`: every corpus directory under
`tools/gate_reference_census.py` is measured, and the file would have entered
the census as a `false-reject`, the one bucket that is empty and stays empty.
Section 6.1 of the first note is that argument, and it ends "a fixture belongs
there when slice 3 lands".

After this branch, the same three programs:

| program | reference | gate |
| ------- | --------- | ---- |
| the control | admits | `''` |
| the admitting placement | admits | `''` |
| the refusing placement | refuses `G-MODEL-PLACE` | `MODEL\|action \`classify\` (Classifier) routes the \`confidential\` origin to model role \`cloud\`, which is declared \`off_device\` on line 2: a confidential input may not leave the device (G-MODEL-PLACE)` |

The message is the reference's, verbatim. That is the contract the crate
promises and the reason the tag alone is not enough: a consumer acts on the
message.

### 1.1 What the port decides

All ten rules `model_route.check` raises, in the reference's own order: the
role table first (`roles()` runs before a single component is looked at), then
each component in declaration order, each block in declaration order, each arm
in declaration order. Plus the prelude ordering rule, which lives in
`lower.py`'s `ModelRouteStmt` branch and reuses the wording every other prelude
declaration carries, so the gate answers it `PRELUDE` exactly as it answers a
misplaced `isolate`.

The arm rules are measured against the reference on twenty-one programs
covering every one of them, both refusing and admitting, in
`tests/test_selfhost_lower.py` and in the `.rvl` file's own `test` blocks. The
in-file blocks are not decoration: `test_in_file_test_programs_agree` harvests
every program literal out of the tests section and routes it through the
differential oracle, so a message eyeballed in `selfhost/lower.rvl` cannot
drift from the reference's.

### 1.2 What the port deliberately does NOT decide

**The value side (item 514).** It walks taint through a body to a `model.*`
crossing. The gate has no flow walk, and it is not getting one in this slice.
Its messages all open "a `<origin>` value reaches the model crossing", and two
of them end in the same "may not leave the device (G-MODEL-PLACE)" tail the
declaration refusal ends in. So the oracle's classifier is written as a set
of POSITIVE markers (substrings the gate spells byte for byte) with that
opening explicitly excluded. Naming those messages would claim an agreement
that does not exist and would report a no-objection the gate is entitled to as
a bypass.

**The crossing side (slice 4, below).** Same reason: it reads a capability
token in a flow position.

**A block written in a form the port cannot read.** This one is a REFUSAL, not
a step-over, and the direction is the whole point. `route model on <action> {`
whose arms are not `<origin> -> <role>` separated by `,` or `;` is answered
`MODEL|`route model on <action>` in <C> is written in a form this gate does
not decide`. Stepping over it instead would mean a block whose arms the gate
skipped is a block whose arms it did not decide, and raising no objection to
one would let a placement the reference refuses through a gate that claims to
decide placements. The crate is allowed to err toward refusing and is not
allowed to err the other way.

That arm is not hypothetical. Roadmap item 515 proposes `<origin> -> a | b | c`
(an ordered candidate set) and item 519 proposes `reaches [...]` on a role;
both are open on this surface. When one lands, the gate refuses those blocks by
name until the port is extended, which is visible in the census the moment a
corpus file uses the form, rather than silently admitting them.

### 1.3 Forward compatibility, stated rather than hoped for

`model role NAME <residence>` is four tokens today, and items 515 and 519 both
propose an optional clause after the residence. `model_role_end` therefore
advances STRUCTURALLY, past a balanced `[...]` or `{...}` and stopping at the
next declaration head or at any keyword, rather than by line or by a fixed
token count. A clause added in that slot is stepped over whole instead of
leaving the cursor mid-clause, which is what would otherwise turn a shipped
program back into `unexpected token at top level`. The gate carries no verdict
for such a clause; that is the answer it gives every construct it does not
decide, and it is the honest one.

The same applies in `selfhost/checker.rvl`.

### 1.4 The two crates, and why both

`selfhost/lower.rvl` is a gate-crate digest input, so `crates/revl-gate` and
`crates/revl-gate-wasm` are both regenerated. `tools/build_gate_crate.py
--check` is the authoritative answer to whether a change is a digest input, and
it reported drift on `GENERATED.json`, `README.md`, `src/admission.rs`,
`src/frontier.rs` and `src/selfhost.rs`: the two ids (`FRONTIER_ID`,
`SURFACE_ID`) plus the emitted rust. Both drift gates compare BYTES ONLY, so a
byte-correct regeneration can still fail to compile; `tests/test_gate_crate_admit.py`
is the one that shells `cargo`, and it passes (314 passed, 2 xfailed),
including `test_the_two_engines_agree`.

---

## 2. Slice 3: the census and the conformance matrix

The census is the measurement that matters, because it is the one that can see
a regression no unit test would.

    464  agree-admit                (463 before: `examples/model_placement.rvl`)
      1  agree-refuse/MODEL         (new: the rejection fixture)
      6  false-admit/T1             (unchanged, baselined, not this item's)
      3  false-admit/TYPE           (unchanged, baselined, not this item's)
         false-reject               EMPTY, before and after

`gate_reference_census.py --check` reports no change from the baseline, and no
re-recording was needed: the baseline records DIVERGENCES only, and both new
rows are agreements.

Two fixtures moved into the corpus, which is what slice 3's oracle was defined
to be:

* `examples/model_placement.rvl`, the admitting program. It is the one
  section 6.1 said could not be written down until the gate decided the
  construct.
* `examples/rejections/gmodelplace_confidential_off_device.rvl`, item 512's
  exit test verbatim.

**The acknowledgement is gone.** `tools/tier_guarantees.py` carried an
`ACKNOWLEDGED` entry for `G-MODEL-PLACE` whose own text read "Remove this entry
when slice 3 lands; a stale acknowledgement fails this gate." It is removed,
and `docs/conformance.md` regenerated: the row went from `no repro` across
every tier to `proved` across every tier.

### 2.1 One change to how the `revl` column is computed

`selfhost_verdicts` credits a reproducer when the gate's tag equals the
guarantee CODE. Almost every family the gate decides is code-less in the
reference (`PRELUDE`, `ROUTE`, `SPAWN`, `HANDOFF`, `BOOT`), so none of them
ever indexed a row in that matrix, and the rule had never been tested against a
family that both carries a code and is tagged by its own name. Item 512's is
the first.

Without a decision the cell read `unimplemented`, whose own definition in that
file is "the gate raises no objection at all, or refuses for an unrelated
reason". Neither is true here: the gate refuses the reproducer with the
reference's own sentence. So `SELFHOST_TAG_CODES` maps `MODEL` to
`G-MODEL-PLACE`, as a table rather than as a prefix rule, so a new tag has to
be DECIDED there, because a tag that silently matched a code would be the
fail-open direction of the same question. The generated reason names the tag a reader
will actually see on the wire.

---

## 3. Slice 4: which roles an action DOES reach

Slice 1 decides what an author may WRITE. Item 514 decides what a VALUE may
do. Neither decides where a call actually GOES, and the difference is the one
section 9 of the first note hands to item 515: "a scheduler that picks a role
no arm names is the fail-open shape, and S4 is what makes that refusable".

### 3.1 How a crossing carries a role

The first note names the spelling and it needs no new syntax: the existing
`model.<role>` capability token (item 343), which already parses. A
`model.<tail>` capability names a ROLE exactly when `<tail>` is a declared
`model role`.

That reading is opt-in by construction and cannot disturb a shipped program.
`model.complete` is an OPERATION token and stays one in every compilation that
does not declare a role called `complete`; a program that declares no role at
all has no crossing that carries one, and nothing moves for it.

The ambiguity is real and is left visible rather than resolved by a second
syntax. Naming a role after an operation word makes that operation token a
placement. It is the author's own choice of name, it is written in the program,
and the alternative, a separate spelling for "this crossing is on role R",
would be a second vocabulary for a fact item 343's token already carries. A
parameterised capability (item 294, `model.local(calls=3)`) is read by its
token head, the way every other reader of a capability token reads one.

### 3.2 The rule

An action that wrote a `route model` block reaches exactly the roles that
block names. A crossing placed on any other role is refused:

```revl reject G-MODEL-PLACE
model role local on_device
model role edge on_device

extern emission[model.edge] fn ask(p: Str) -> Int = @py { return 0 }

service Answer { emission fn classify(text: Str) -> Int }

component Classifier provides out: Answer {
  route model on classify { web -> local }
  provide out {
    fn classify(text) {
      let r = ask(text)
      return r
    }
  }
}
```

    the model crossing `ask` (`model.edge`) in action `classify` (Classifier)
    is placed on model role `edge`, which no arm of its `route model` block
    names: the block reaches `local` (G-MODEL-PLACE)

The diagnostic names the action, the role and what the block DOES reach, which
is the shape item 512's exit asks of every refusal on this surface.

`*` is the one reading that could go wrong here too, and the answer is the
mirror of section 2.1 of the first note: `*` widens the ORIGINS an arm covers,
never the ROLES a block names. If `* -> local` meant "any origin to any role"
the block would place nothing at all.

### 3.3 Which way it fails, and what it does not do

Toward refusing, and it never widens. The block is the complete list, so the
answer to "this crossing goes somewhere the block does not name" is a refusal
and not an added arm.

Two things are deliberately untouched, and both are the same line `admits()`
already draws:

* **An action with no block.** A program that declared no placement for this
  action is judged by the rules that judged it before item 512. Making
  `route model` mandatory would be a different item; this one adds refusals,
  not obligations. Note this is a NARROWER line than item 514's: the ceiling
  refuses a confidential value in an unrouted action of a ROUTED component
  (`unrouted`), because a confidential input is the thing that can actually be
  lost. A crossing carrying no confidentiality origin into an action nobody
  placed is the pre-512 state of the world, and refusing it would be a
  different item again.
* **A program that declares no `model role`.** Nothing can be placed on a role
  that does not exist, so `role_of_crossing` reads every `model.*` token as the
  operation name it has always been.

The rule runs AFTER the origin ceiling at both call sites. A crossing can be
both misplaced and carrying a confidential value, and the value side is the
half a confidential input can actually be lost through, so its diagnostic is
the one the author should see first.

### 3.4 The one line written for a surface that has not landed

`reach_of` reads a `candidates` key on a placement. No placement carries one
today. Roadmap item 515 turns an arm into an ordered candidate set
(`<origin> -> a | b | c`), and every candidate in a set is a role the action
may reach, so reading the key now means the reach widens with that surface the
moment `check()` records it, rather than this rule refusing a fallback the
program plainly names. An over-refusal of a written intent is a defect in the
opposite direction from the one this item guards, and it is worth one `if` to
make it unreachable. `test_reach_of_reads_a_candidate_set_when_a_placement_carries_one`
pins the behaviour so the key cannot be dropped by a later edit that does not
know why it is there.

### 3.5 Why the walk engages on a block

`_TaintModel.active` gates the flow walk: a program with no qualifier and no
endorse slot engages nothing and stays byte-identical. `model_routes` is now
one of the things that engages it, and it is the odd entry there because it is
not a taint surface.

The reason is the rule's own scope. The crossing side asks where a call GOES,
which is a property of the capability token and the block; no value has to be
tainted for the question to have an answer. Gating it behind a qualifier would
have produced a placement rule that fires only on the programs that happen to
carry one: silently inactive over most of its own surface, which is the
fail-open shape the item exists to remove. A program with no `route model`
block is unaffected, so nothing that compiles today starts walking.

### 3.6 The guarantee text was already right

`G-MODEL-PLACE` was registered at slice 1 as:

> a model role declared `off_device` never receives a confidentiality origin,
> and an action reaches only the roles its `route model` block names

The second clause was written ahead of its enforcement. Slice 4 is what makes
it true, so no registry entry moves and `docs/rejections.md` is unchanged.

---

## 4. Non-goals

* **Porting the crossing side or the value side to the self-host.** Both read a
  flow position; the gate has no flow walk. It raises no objection to either,
  which is a no-objection and not an admission, and the oracle's classifier
  names neither so the gap is a measured out-of-slice row rather than a claimed
  agreement.
* **Making `route model` mandatory.** Section 3.3.
* **Scheduling.** Item 515 schedules inside the boundary slice 4 closes. What
  slice 4 buys item 515 is that the boundary cannot be widened from outside it.
* **A second spelling for "this crossing is on role R".** Section 3.1.
* **Reconciling the open clause proposals.** Items 515 and 519 are in a
  reconcile sequence of their own on `model role`'s optional clauses. This note
  assumed the clause syntax ON `origin/main` at `dfecba2a` (`model role NAME
  <residence>` and `<origin> -> <role>`) and made both readers survive the
  proposals rather than anticipate one: section 1.3 for the declaration,
  section 3.4 for the arm.

---

## 5. Things stated here that are not verified

* That the gate agrees with the reference on every `route model` program, as
  opposed to on the twenty-one measured and the corpus. The declaration half is
  small and closed, and every rule has a measured case both ways, but that is
  an enumeration of the rules rather than of the programs.
* That `model.<role>` is the spelling item 515 will want. It is the one the
  first note names and the one item 343's token already carries, and it needs
  no grammar; whether a portfolio wants to write a placement some other way is
  515's question.
* That the reach rule sees every crossing a routed action reaches. It fires
  where item 514's ceiling fires, which is the direct crossing and the
  transitive one recorded on `_Signature.models_at`. A crossing that takes no
  argument is not recorded there and so is judged only at its direct site: the
  same limit item 514 carries, inherited rather than introduced, and the first
  note's section 10 already records it for the ceiling.
* Anything about tiers other than the reference. Neither slice reaches an
  emitter and the placement still writes no IR, so there is nothing
  tier-specific to be right or wrong about, but that is an argument and not a
  measurement.
