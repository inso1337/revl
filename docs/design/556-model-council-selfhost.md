# 556: the self-host port of `model council`

Roadmap item 516 (issue #1190) landed the declaration in PR #1257. The
self-host side was not touched, and issue #1291 is the follow-up the lane
closing item 512 slice 3 flagged rather than absorbed. `docs/design/543-model-council.md`
designs the construct and is the note to read first; this one records only
what the port decided, measured, and deliberately left alone.

Reconciles with: `docs/design/543-model-council.md` (the design),
`docs/design/531-model-placement.md` and `docs/design/554-route-model-remaining.md`
(item 512, whose `model role` table a council indexes and whose port this one
copies), item 391 (the self-host frontier), `tools/gate_reference_census.py`
(the verdict census the port is measured in), and `docs/conformance.md` (the
guarantee x tier matrix).

---

## 1. What the gate answered before, and what it answers now

Both `selfhost/lower.rvl` and `selfhost/checker.rvl` answered ANY program
declaring a council with `BAD|unexpected token at top level`, the generic
top-level parse marker. Two separate `p_top` functions, failing the whole
document independently.

That is the fail-safe direction: the gate never admitted a council it could not
decide. It was still two things worth removing.

The marker was generic, so a consumer reading the wire learned only that
something at top level did not parse, not which construct the gate declined.
And because the refusal was generic, an ADMITTING council could not be put in
`examples/`: every corpus directory under `tools/gate_reference_census.py` is
measured, and the file would have entered the census as a `false-reject`, the
one bucket that is empty and stays empty. It was not red on the day it was
written only because no file under a corpus root used the construct; the first
council example or fixture a later slice added anywhere in `examples/`,
`tests/fixtures/`, `stdlib/`, `demo/`, `tck/`, `backends/` or `dogfood/` would
have made it one.

After this branch:

| program | reference | gate |
| ------- | --------- | ---- |
| a component with no council (control) | admits | `''` (admits) |
| a three-member council with a written `aggregate` | admits | `''` |
| `aggregate unanimous on_tie allow` | refuses `G-COUNCIL-SPLIT` | ``COUNCIL|`on_tie allow` in model council `Release` admits when the members disagree`` |

The message is the reference's, verbatim. That is the contract the crate
promises and the reason the tag alone is not enough: a consumer acts on the
message.

## 1.1 Why the tag is `COUNCIL` and not `MODEL`

Item 512's port introduced `MODEL` for the model-PLACEMENT family. When this
port was written a council raised under the same registered code,
`G-MODEL-PLACE`, because `revl.model_council` imported `model_route.CODE`. It
would therefore have been possible to widen `MODEL` to cover both.

It is a separate tag because the gate tags by the FAMILY a refusal belongs to,
and these are two constructs with two reference modules and two disjoint rule
sets: `src/revl/model_route.py` decides where an action's model calls run, and
`src/revl/model_council.py` decides what a declared member set and its
aggregation may say. A consumer reading `COUNCIL|...` off the wire learns which
of the two was refused without parsing the sentence.

That reasoning survived issue #1190, which registered `G-COUNCIL-SPLIT` and
moved eleven of the thirteen council refusals onto it, keeping the two whose
subject is a `model role` under `G-MODEL-PLACE`
(`docs/design/557-council-disagreement.md` section 3). The tag names the
construct and the construct did not move. What changed is that the tag is no
longer a name for one code.

`SELFHOST_TAG_CODES` in `tools/tier_guarantees.py` therefore maps a tag to a
SET, and `COUNCIL` maps to both codes. Measured on this branch, with the row
removed and the corpus fixture in place, the `revl` column of the guarantee x
tier matrix reads:

    unimplemented: the self-host gate answers every G-COUNCIL-SPLIT
                   reproducer under COUNCIL

for a reproducer the gate refuses with the reference's own sentence, which is
the same false reading this row was added to fix, now on the other code. With
the row it reads `proved ... under COUNCIL`.

Writing `COUNCIL -> G-MODEL-PLACE` alone would not have fixed it, and on the
tag-only test this port used it carried a second fault: it credits item 512's
row for ANY refusal the gate tags `COUNCIL`. Eleven of the thirteen council
rules are not item 512's, so a placement fixture the gate decided by one of
them would still have read `proved` for `G-MODEL-PLACE`, on evidence that
belongs to the other code. No corpus fixture exercises that today, which is
why the rendered matrix is the same with that row and without it.

Being in the set is not on its own enough to credit a cell. A refusal resolved
through this table is credited only when the gate's MESSAGE is the reference's
message byte for byte, which is the agreement test
`tools/gate_reference_census.py` already applies to this same corpus. That is
what keeps the set from being a guess: the tag says which family, the sentence
says which of the family's guarantees, and `tools/tier_guarantees.py` never
restates the rule that splits them. The table stays a TABLE rather than a
prefix rule for the reason item 512 gave, so a new tag still has to be decided
there.

## 1.2 What the port decides

All seventeen refusals `model_council._check_one` raises, in the reference's
own order: the name (declared once, and not a `model role`'s), then each member
in declaration order (the function vocabulary, one member per function, the
role's existence, one member per role), then the two counting rules over the
member set (at least two members, and a `proposer` among them), then the
aggregation clause (exactly one, written down; the rule itself, with the
pick-one spellings refused ahead of the vocabulary check so an author who
reaches for one gets the REASON; `veto` needing the adversary it is the veto
of; the quorum basis; the tie outcome).

The phase runs where `check_and_lower` runs `model_council.check`: after
`model_route.check` and before any component is lowered. That ordering is
observable. A program with both a malformed role table and a council is refused
as a PLACEMENT, under `MODEL`, not as a council naming an undeclared member.

The rules are measured against the reference on the programs in
`selfhost/lower.rvl`'s own `test` blocks, which are not decoration:
`tests/test_selfhost_lower.py::test_in_file_test_programs_agree` harvests every
program literal out of the tests section and routes it through the differential
oracle, so a message eyeballed in the `.rvl` file cannot drift from the
reference's.

## 1.3 What the port deliberately does NOT decide

**Binding a council to an action.** SUPERSEDED by slice 2 (issue #1366); kept
because the reason it gave was half right and the half that was wrong is worth
naming. The sentence read: slice 2 turns a `route model` arm naming a council
into a placement, it reads a flow position, the gate has no flow walk, and it
is not getting one here.

Slice 2 landed as two rules and only one of them reads a flow position. The
DECLARATION half is an arm the author wrote, resolved against a council whose
members' residences are declared in the same token stream, so the gate decides
it: an arm naming a council, the `secret` origin against a council at any
placement, and a confidentiality origin against a council one of whose members
is `off_device`, that last one naming the MEMBER. All three live in the
model-PLACEMENT section under the tag `MODEL`, because what they refuse is a
`route model` arm and the arm is item 512's surface; the council is only what
the arm names.

What still reads a flow position, and is still not decided here, is item 514's
ceiling on a council-placed VALUE. Its messages open "a `<origin>` value
reaches the model crossing", which is the opening the oracle's classifier
already excludes, and the exclusion needed no change for the council spelling
of it.

Slice 2 also MOVED A PHASE. `model_council.check` now runs ahead of
`model_route.check` in the reference, because an arm may name a council and a
placement is resolved before anything that names one, so `collect_nonlink`
swapped the two verdicts to match. `model_council_refusal` gained the role-table
validation the reference reaches through `model_council.check`'s own call to
`model_route.roles`, or a program declaring both a malformed role and a
refusable council would have answered `COUNCIL` here and `MODEL` there. Two
in-file tests pin the new order and one that pinned the old one was rewritten.

**What the members answered.** Item 517's evidence record, and the `Aggregate[T]`
runtime of the design's section 3, are values and not declarations. Nothing in
this port contradicts that shape and nothing implements it: `Agreed(T)` is the
only arm that carries a `T`, `Dissent` carries per-member answers as DIGESTS,
and the quorum floor is counted over the DECLARED members. The port enforces
the declaration-side half of that last sentence (`quorum answered` is
refused by name) and is silent on the rest.

The oracle's classifier is therefore written as a set of POSITIVE markers,
substrings the gate spells byte for byte, each shaped by an opening plus the
phrase that fixes the rule. Naming a sentence the gate does not decide would
claim an agreement that does not exist and would report a no-objection the gate
is entitled to as a bypass.

**A body written in a form the port cannot read.** This one is a REFUSAL, not a
step-over, and the direction is the whole point. A `model council NAME {` whose
items are not `<function> -> <role>` or
`aggregate <rule> [quorum <basis>] [on_tie <outcome>]`, separated by `,` or
`;`, is answered ``COUNCIL|model council `<N>` is written in a form this gate
does not decide``. Stepping over it instead would mean a body whose items the
gate skipped is a body whose members and aggregation it did not decide, and
raising no objection to one would let an `on_tie allow` through a gate that
claims to decide councils. The crate is allowed to err toward refusing and is
not allowed to err the other way.

That arm is not hypothetical. Item 517 proposes recording what each member
answered and item 519 proposes a clause on a role; either may put a word in
this body the reader does not know. The gate then refuses the council by name
until the port is extended, which is visible in the census the moment a corpus
file uses the form, rather than silently admitting it.

## 1.4 Forward compatibility, stated rather than hoped for

`model council NAME { ... }` is recognised by the pair `model council` plus a
name plus the body brace, which is also what tells it apart from
`model role NAME <residence>`: the fourth slot is a brace rather than an
identifier. `model` and `council` both stay ordinary identifiers, so neither
lexer's KEYWORDS table moves and a program using either as a name keeps
parsing.

The STEP past a council is the match of its body brace, not a token count and
not a line, so a clause a later item adds inside the braces is stepped over
whole instead of leaving the cursor mid-clause. A body whose braces do not
balance is a reference parse refusal this gate carries no verdict for, so the
cursor advances one token and the ordinary top-level refusal stands rather than
a verdict being invented.

The same applies in `selfhost/checker.rvl`, which carries its own `p_top` and
whose step is guarded by
`tests/test_checker_reference_census.py::test_no_admitted_document_fails_at_the_top_level_parse`.
`model_role_end` there also learned to stop at a council head, or a `model role`
written immediately before one would have swallowed it.

## 1.5 The two crates

`selfhost/lower.rvl` is a gate-crate digest input, so `crates/revl-gate` and
`crates/revl-gate-wasm` are both regenerated. `tools/build_gate_crate.py --check`
is the authoritative answer to whether a change is a digest input, and it
reported drift on `GENERATED.json`, `README.md`, `src/admission.rs`,
`src/frontier.rs` and `src/selfhost.rs`. Both drift gates compare BYTES ONLY,
so a byte-correct regeneration can still fail to compile;
`tests/test_gate_crate_admit.py` is the one that shells `cargo`, and it passes
(314 passed, 2 xfailed), including `test_the_two_engines_agree`.

---

## 2. The census

    465  agree-admit                (464 before: `examples/model_council.rvl`)
      1  agree-refuse/COUNCIL       (new: the rejection fixture)
      1  agree-refuse/MODEL         (unchanged, item 512's)
      6  false-admit/T1             (unchanged, baselined, not this item's)
      3  false-admit/TYPE           (unchanged, baselined, not this item's)
         false-reject               EMPTY, before and after

852 programs, 850 before. `gate_reference_census.py --check` reports no change
from the baseline, and no re-recording was needed: the baseline records
DIVERGENCES only, and both new rows are agreements.

Two fixtures moved into the corpus, which is what the port was defined to make
possible:

* `examples/model_council.rvl`, the admitting program, with the two optional
  clauses written at their admitted values.
* `examples/rejections/gcouncilsplit_on_tie_allow.rvl`, item 516's exit test
  verbatim: an aggregation written to admit when the members disagree. The
  reference refuses it under `G-COUNCIL-SPLIT`, so it is that row's reproducer
  rather than a second one for `G-MODEL-PLACE`.

One `ACKNOWLEDGED` entry moved: `G-COUNCIL-SPLIT`'s. Issue #1190 wrote it with
its own expiry ("remove this entry when the self-host port lands, issue
#1291; a stale acknowledgement fails this gate"), and its second reason, that
a fixture could not live in `examples/rejections/` because the self-host
answered any `model council` program `BAD|unexpected token at top level`, is
what this port retires. The fixture above is that reproducer, so the entry is
gone and the row carries itself. `G-MODEL-PLACE`'s entry was already removed
by item 512 slice 3.

---

## 3. Things stated here that are not verified

* That the gate agrees with the reference on every council program, as opposed
  to on the twenty-six measured and the two corpus files. Every rule has a
  measured case, and the admitting cases cover both separators, the trailing
  separator and both optional clauses, but that is an enumeration of the rules
  rather than of the programs.
* That `COUNCIL` is the tag a consumer wants. It is the one that names the
  construct; whether a consumer would rather switch on the code and read the
  construct out of the message is a question the wire format has not been asked.
* Anything about tiers other than the reference. The port reaches no emitter
  and a council still writes no IR, so there is nothing tier-specific to be
  right or wrong about, but that is an argument and not a measurement.
