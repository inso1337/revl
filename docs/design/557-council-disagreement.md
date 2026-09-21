# 557: Disagreement is never resolved toward allow

Roadmap: item 516 (issue #1190), from the 2026-09-19 external review. This note
lands the CHECKED PROPERTY the issue names and the guarantee code that carries
it. It does not re-land the surface.

Design number: 557, assigned by the orchestrator.

Builds on: item 516 and `docs/design/543-model-council.md` (the `model council`
declaration and its thirteen refusals, all of which this note keeps), item 512
and `docs/design/531-model-placement.md` (the `model role` each member names,
and the code two of the thirteen keep), item 514 (the origin ceiling that makes
separate placement mean something), item 523 and `tools/tier_guarantees.py`
(the generated guarantee x tier matrix this registers a row in), item 471 and
`docs/design/471-quorum-approval.md` (multi-party HUMAN approval, which this is
not, and which shares no code with it).

---

## 0. The decision in one paragraph

Item 516 landed the council declaration and every rule that keeps it honest,
and filed all thirteen refusals under item 512's `G-MODEL-PLACE`. The rules are
right and this note changes none of them. What it changes is the code eleven of
them carry, because a diagnostic code in this repository is not a label: it is
the `guarantee` line and the `fix` line `revl.diagnostics.classify()` hands an
agent so it can react to a refusal WITHOUT parsing prose, and item 512's pair
says to route the origin to a role declared `on_device`. That is not the
rewrite for `on_tie allow`, for `quorum answered`, or for a council with no
`aggregate` clause at all. So the disagreement rules get their own code,
`G-COUNCIL-SPLIT`, with a guarantee line that states the property and a fix
line that names the three clauses an author can write. The property itself is
unchanged and was already enforced:

> **disagreement can never be silently resolved toward allow** - an aggregation
> that admits on a tie, or that admits while a member is unreachable, is
> refused.

---

## 1. The gap this closes

The issue asks for a checked property. Item 516 checked it. What was missing is
everything that makes a checked property VISIBLE to something other than a
human reading `src/revl/model_council.py`:

| | before | after |
| - | ------ | ----- |
| a guarantee code | `G-MODEL-PLACE`, item 512's | `G-COUNCIL-SPLIT`, registered in `revl.diagnostics.GUARANTEES` and `FIXES` |
| what `revl explain` answers | item 512's placement sentence | the disagreement sentence and the three clauses |
| a reproducer set | the item-516 suite, filed under item 512's code | `tests/test_council_disagreement_1190.py`, plus the fences in section 4 below |
| a row in the tier matrix | none; the property was invisible to `tools/tier_guarantees.py` | one row, `proved` on all seven tiers, with `examples/rejections/gcouncilsplit_on_tie_allow.rvl` as its reproducer |

The middle row is the defect and not the bookkeeping. `src/revl/diagnostics.py`
opens by saying the module is "the machine-facing view ... so an agent can
react to a rejection without parsing prose". On the tree before this note, an
agent that wrote `aggregate unanimous on_tie allow`, read the structured
record, and applied the `fix` it was handed would have gone looking for an
origin to re-route and would never have touched the clause it was refused for.

---

## 2. Why a second code, and not a thirteenth rule under item 512's

`docs/design/543-model-council.md` section 8 argued the other way, and the
argument was reasonable: the council is the placement family's second
construct, every rule is about which roles a declaration binds, and a new code
costs either a fixture in a census corpus root or an `ACKNOWLEDGED` entry. Two
things it did not weigh:

**A code is a pair, not a name.** `GUARANTEES[code]` and `FIXES[code]` are
both copied into every classified refusal, and both are read by
`revl explain`, by the LSP (`src/revl/lsp/analysis.py`), by the MCP surface
(`src/revl/mcp/server.py`) and by the evolve curriculum
(`tools/evolve_curriculum.py`). One code with two unrelated fix lines is not
available; the only way to give a refusal the right rewrite is to give it a
code. The council's fix line has to name `aggregate`, `quorum` and `on_tie`,
and item 512's has to name `route model` and `on_device`, so there are two.

**The property is not a placement property.** "Disagreement is never resolved
toward allow" is a statement about an aggregation over answers. It is true of a
council all of whose members are `on_device`, where no placement question
arises at all. Filing it under the placement code makes item 523's tier matrix
report the placement rule's coverage as if it were this one's, which is the
"nothing checked it" / "it passed" collapse `docs/conformance.md` already
refuses to make for toolchains.

The cost 543 named is real. This note first paid it with one `ACKNOWLEDGED`
entry carrying the condition that removes it; section 6 records that condition
firing and the fixture taking over.

---

## 3. The line between the two codes

One sentence, and it decides every row:

> A council refusal carries `G-MODEL-PLACE` exactly when its subject is a
> `model role` - one this program does not declare, or one whose name the
> council also claims. Every other council refusal carries
> `G-COUNCIL-SPLIT`, because what it refuses would let the council report
> agreement it does not have.

Applied to `docs/design/543-model-council.md` section 8's thirteen rows:

| # | the decision | code | why that code |
| - | ------------ | ---- | ------------- |
| 1 | a council name is declared once | `G-COUNCIL-SPLIT` | two member sets under one name make WHICH members agreed depend on declaration order |
| 2 | a council name is not also a model role name | `G-MODEL-PLACE` | two placement tables would both answer the word; the rewrite is to rename the role or the council |
| 3 | a member's function is in the closed vocabulary | `G-COUNCIL-SPLIT` | a member with no job in the aggregation is counted by nothing |
| 4 | a function is declared at most once per council | `G-COUNCIL-SPLIT` | the count depends on order, so the floor is not a number |
| 5 | a member's role is a declared `model role` | `G-MODEL-PLACE` | the subject is the role; the fix is to declare it, which is item 512's fix line verbatim |
| 6 | no two members name the same role | `G-COUNCIL-SPLIT` | two agreeing answers from one placement is one model answering twice: agreement the council does not have |
| 7 | a council declares at least two members | `G-COUNCIL-SPLIT` | a council that cannot disagree always agrees with itself |
| 8 | a council declares a `proposer` | `G-COUNCIL-SPLIT` | with nothing proposed the aggregation has no value to name, so it is not total on anything |
| 9 | a council declares exactly one `aggregate` | `G-COUNCIL-SPLIT` | none is the implicit aggregation; two is order deciding between rules that can disagree |
| 10 | the rule is in the closed vocabulary, and `first`/`any`/`fastest`/`cheapest`/`best`/`random` are refused by name | `G-COUNCIL-SPLIT` | a rule that picks a member is the silent resolution itself |
| 11 | `aggregate veto` declares an `adversary` | `G-COUNCIL-SPLIT` | a veto over a member that does not exist is a protection the council does not have |
| 12 | `quorum` is `declared`, and `answered`/`reachable`/`available`/`responding` are refused by name | `G-COUNCIL-SPLIT` | the issue's second half: a floor over the answering set admits while a member is unreachable |
| 13 | `on_tie` is `split` or `deny`, and `allow`/`admit`/`proceed`/`accept`/`first`/`any` are refused by name | `G-COUNCIL-SPLIT` | the issue's first half, and the one direction that must never exist |

Row 6 is the one worth arguing with, because its message names a role. It is
here rather than under item 512 because the role in it is PERFECTLY placed: the
rewrite is to give the second member a different role, not to fix a placement,
and what the refusal protects is the council's arithmetic. Row 2's role, by
contrast, is genuinely ambiguous to the placement tables, which is item 512's
own failure mode.

`src/revl/model_council.py` holds the split in two functions, `_err` and
`_place_err`, so a new rule has to choose one and the choice is visible in the
diff.

---

## 4. The four adversarial shapes

Written before the code that answers them, which on this item meant written
against a checker that already answered three of the four. Each fence below is
compiled by `tests/test_doc_examples.py` and its code is asserted, so these are
reproducers and not illustrations. The message is the other half of the
contract: agreement in this repository is on tag AND message
(`tools/gate_reference_census.py` buckets `msg-mismatch` separately from
`agree-refuse`), and `tests/test_council_disagreement_1190.py` pins the text.

### 4.1 A tie that admits

```revl reject G-COUNCIL-SPLIT
model role edge on_device
model role aux  on_device
model role vast off_device

model council Release {
  proposer  -> vast,
  adversary -> edge,
  verifier  -> aux,
  aggregate unanimous on_tie allow
}
```

    `on_tie allow` in model council `Release` admits when the members disagree
    (G-COUNCIL-SPLIT)

All six admitting spellings parse and are refused by name. A spelling that
produced a syntax error would leave the author guessing that the feature is
unimplemented rather than refused.

### 4.2 A member that is unreachable

```revl reject G-COUNCIL-SPLIT
model role edge on_device
model role aux  on_device
model role vast off_device

model council Release {
  proposer  -> vast,
  adversary -> edge,
  verifier  -> aux,
  aggregate majority quorum answered
}
```

    `quorum answered` in model council `Release` counts the rule's floor over
    the members that answered (G-COUNCIL-SPLIT)

This is the shape that reads as pragmatism about flaky hosts. Two members of
three agreeing while the third is silent is not agreement, and a floor counted
over the answering set lets a council shrink until its survivors agree. The
admitted basis is `declared`, it is the default, and a member that fails or
times out abstains AGAINST the floor.

### 4.3 An aggregation that is not total

```revl reject G-COUNCIL-SPLIT
model role edge on_device
model role vast off_device

model council Release {
  proposer  -> vast,
  adversary -> edge
}
```

    model council `Release` declares no `aggregate` rule (G-COUNCIL-SPLIT)

An implicit aggregation is the defect the issue names. With no rule written
down, disagreement is resolved by whatever the caller does with the members,
which is the silent pick the construct exists to remove. `aggregate first` and
its five siblings are the same defect written down, and are refused with a
message that says so rather than with a closed-vocabulary complaint.

Totality itself is checked rather than asserted:
`test_every_admitted_rule_is_total_over_the_declared_set` walks the three
admitted rules and requires, for each, a floor that is a strict majority of the
DECLARED members and a tie outcome that names no value.

### 4.4 Members that share a placement

```revl reject G-COUNCIL-SPLIT
model role edge on_device
model role aux  on_device
model role vast off_device

model council Release {
  proposer  -> vast,
  adversary -> edge,
  verifier  -> edge,
  aggregate unanimous
}
```

    members `adversary` and `verifier` of model council `Release` are both
    placed on model role `edge` (G-COUNCIL-SPLIT)

A council must not flatten its members into one placement. Two answers from one
role are one model answering twice under two names: same weights, correlated
errors, and a unanimity that was never tested.

---

## 5. Members are placed separately, and that is measured

The motivating case in the issue is a LOCAL adversary reading an origin the
cloud proposer is not allowed to see, so the placement distinction is the point
and not a detail. `test_each_member_keeps_its_own_admission_ceiling` reads it
off one admitted council rather than describing it:

| member | role | residence |
| ------ | ---- | --------- |
| `proposer` | `vast` | `off_device` |
| `adversary` | `edge` | `on_device` |
| `verifier` | `aux` | `on_device` |

Each member's admission ceiling is its own, by item 512's rules. The COUNCIL's
own ceiling is separate from all three and is the most permissive of them, not
the least, because giving an input to a council gives it to every member; that
number is what item 516 slice 2 reads and
`test_the_councils_own_ceiling_is_the_most_permissive_member` pins both
directions.

---

## 6. The tier matrix: an acknowledgement that expired on schedule

`tools/tier_guarantees.py` builds the guarantee x tier matrix from the
compiler's own register, and a host-tier cell that is not `proved` must be
listed in `ACKNOWLEDGED` with a reason. Its reproducer source is
`examples/rejections/*.rvl` and nothing else, so a new code either lands a
fixture there or is acknowledged.

This note was first written with the acknowledgement, because a fixture was
not available: `examples/rejections/` is a census corpus root
(`tools/gate_reference_census.py` `CORPUS_DIRS`, walked with `rglob`) and
`selfhost/parser.rvl` answered any program that declares a `model council`
with `BAD|unexpected token at top level`, so a council fixture in that
directory would have entered the census as a divergence against a self-host
that cannot read it. That was item 512's decision, held for the same reason.
The entry carried its own expiry:

> ... Remove this entry when the self-host port lands (issue #1291); a stale
> acknowledgement fails this gate.

**The port landed and the condition fired.** `selfhost/lower.rvl` now decides
the council declaration and spells every refusal the reference's way
(`docs/design/556-model-council-selfhost.md`), which is what made a corpus
fixture possible, and `examples/rejections/gcouncilsplit_on_tie_allow.rvl`
is it: the reference refuses it under `G-COUNCIL-SPLIT`, the gate answers
``COUNCIL|`on_tie allow` in model council `Release` admits when the members
disagree``, the two sentences are equal byte for byte, and
`tools/gate_reference_census.py --check` buckets it `agree-refuse/COUNCIL`
with `false-reject` empty. So the acknowledgement is gone and the row carries
itself: `G-COUNCIL-SPLIT` reads `proved` on all seven tiers, and the `revl`
cell's reason is "the self-host gate refuses every reproducer for
G-COUNCIL-SPLIT under COUNCIL".

The expiry condition was the point, and it worked in the direction that is
easy to get wrong. `tools/tier_guarantees.py` fails generation in BOTH
directions, so the entry could not outlive its reason: with the port and this
note both in the tree and the entry still present, generation exits 1 with
"ACKNOWLEDGED still excuses G-COUNCIL-SPLIT, which now has a reproducer".

### 6.1 One tag, two codes

The gate tags every council refusal `COUNCIL`, because the tag names the
CONSTRUCT a consumer reading the wire needs to identify, and section 3 above
splits that one construct across two codes. `SELFHOST_TAG_CODES` in
`tools/tier_guarantees.py` therefore maps a tag to a SET of codes rather than
to one code, and `COUNCIL` maps to both.

The set is not read as "any of these". A refusal resolved through that table
is credited to a row only when the gate's MESSAGE is the reference's message
byte for byte, the same agreement test the census applies to the same corpus.
The tag says which family; the sentence says which of the family's guarantees;
`tools/tier_guarantees.py` never restates section 3, so the two cannot drift
apart. Mapping `COUNCIL` to `G-MODEL-PLACE` alone, which is what the port was
written against, would instead have credited item 512's code with reproducers
for programs this note's code refuses.

---

## 7. Not multi-party human approval, and how that is kept true

Items 471 and 509 are operator votes. A vote there comes from a bound operator
with an identity and is a statement about consent; `require N of {a, b, c}`
means three PEOPLE. A council member is a model, carries no identity, and its
answer is evidence rather than consent. The two vocabularies both use the words
"quorum", "majority" and "vote", which is exactly why they have to be kept
apart by something stronger than intent.

They share no code. `src/revl/model_council.py` imports `dataclasses`,
`revl.errors` and `revl.model_route`, and nothing else;
`test_the_council_checker_imports_none_of_item_471s_machinery` reads the import
list with `ast` and pins it to that set, so a later change that reaches for
`revl.mcp.quorum` or `revl.policy` fails rather than merging the two families
quietly. The check reads imports and not text on purpose: the module's own
prose says the word "operator" several times, saying precisely that the two are
different, and a grep would refuse the sentence that keeps them apart.

They also share no prose. The test
`test_a_council_refusal_never_reads_as_a_withheld_approval`
asserts that no council refusal message or hint contains "operator",
"approval", "approve", "consent" or "signer", so a caller cannot read `split`
as "somebody declined".

**Finding, for the record.** The issue places this refusal "in the family of
item 133's quorum work". Roadmap item 133 is the cross-tier agreement theorem
(Lean 4, `formal/RevL/Theorems/CrossTier.lean`), not quorum work, and it has no
machinery a refusal could reuse. The quorum machinery in this tree is item
471's and item 509's, which is the operator-approval family the same issue
says, correctly, must not be conflated with this one. Nothing was reused from
either, and nothing needed to be: the council's floor is `n // 2 + 1` over the
declared members, computed in `Council.floor`, which is four lines and shares
no state with a session's vote ledger.

---

## 8. What this note does not do

It registers a code, gives it reproducers and a matrix row, and splits the
existing thirteen refusals across two codes. It adds no rule, no syntax and no
IR, so an admitted program is still byte-identical to the same program with the
council declaration deleted (`test_the_refusals_cost_the_ir_nothing`).

Item 516's slices 2 to 5 are untouched and still open: binding a council to an
action, the `Aggregate[T]` answer type and its exhaustiveness rule, per-member
inputs, and the self-host port. The roadmap's first exit clause, "a two-member
council that disagrees does not admit", is a statement about a call and belongs
to slice 3; what this note and item 516 deliver together is the second, "an
aggregation written to admit on a tie is refused at compile time", with a code
an agent can act on.

---

## 9. Where it lands

| file | change |
| ---- | ------ |
| `src/revl/diagnostics.py` | `G-COUNCIL-SPLIT` in `GUARANTEES` and `FIXES` |
| `src/revl/model_council.py` | `CODE`/`CATEGORY` are the council's; `_place_err` keeps rows 2 and 5 on item 512's |
| `tools/tier_guarantees.py` | `SELFHOST_TAG_CODES` maps a tag to a SET of codes; the `ACKNOWLEDGED` entry is retired by the fixture (section 6) |
| `tests/test_council_disagreement_1190.py` | the executable spec: the four shapes, the placement measurement, the code pair, the 471 separation |
| `tests/test_model_council_516.py` | the two rows that kept item 512's code, and the tie-back to the module constants |
| `docs/design/543-model-council.md` | section 8's "no new guarantee code" paragraph, superseded here |
| `examples/rejections/gcouncilsplit_on_tie_allow.rvl` | the corpus reproducer, refused under this code |
| `docs/rejections.md`, `docs/conformance.md` | generated (`tools/docgen.py`, `tools/tier_guarantees.py`) |
| `docs/guarantees.md` | one paragraph, beside the placement family's |

No emitter, no golden, no crate: the council still writes no IR.
