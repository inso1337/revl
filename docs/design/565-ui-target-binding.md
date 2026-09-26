# Design: the computer-use target binding and the recorded action

Design-doc id 565. Roadmap item served: 521 (issue #1195), slices 4 and 5 of
the plan in `docs/design/532-typed-computer-use.md` §10. 532 stays the item's
design note; this one exists because slice 4 is the first slice that decides
anything about a computer-use program's DATA rather than its authority, and
532 §5 records the binding in one sentence and then moves on. Slice 5 is here
rather than in a note of its own because the receipt it defines is the target
binding plus one member (§12), and splitting them would put one table's
justification two files away from the other's.

Adjacent and deliberately not restated: 522 (issue #1196, UI transactions,
whose postcondition problem this slice is the named blocker for), 249 (taint
and provenance, which owns the `Untrusted` half of `Untrusted[UiTarget]`),
539 (upstream `inso1337/revl-harness#11`, the substrate that actually
resolves a target), 517 (the model decision as evidence, the shape slice 5's
receipt is measured against).

Sources studied, all at `4cfc8f32`: `src/revl/ui_family.py`,
`src/revl/lower.py` (`_check_and_lower`, `_lower_type_decls`,
`_lower_externs`), `src/revl/taint.py` (`extract_and_normalize`,
`strip_qualifiers`), `src/revl/parser.py` (`type_decl`, `_capability_list`),
`src/revl/retention.py` (`PERSISTENCE_SINK_SCOPES`, `persistence_sink_of`),
`src/revl/gate.py` (`Verdict`), `selfhost/lower.rvl` (`admit_src`),
`tools/build_gate_crate.py` (`frontier_tables`), and PR #1287's own "what is
not verified".

## 0. The decision in one paragraph

A computer-use target is a **record the program declares and revl checks the
floor of**, carried by value from the verb that resolves it to the verb that
acts on it. `ui.find` returns `UiTarget`; `ui.click`, `ui.text` and
`ui.download` each take one. The field set is registry-owned
(`ui_family.TARGET_FIELDS`), for the same reason the reversibility class and
the taint role are: a binding an author can drop is a binding a careless
author drops. revl ships no `UiTarget` type, exactly as it ships no
`ui.click` extern, because the family is host-backed and its data shape is as
OS-specific as its effect surface. What revl owns is that the binding is
there.

## 1. The problem, measured rather than argued

On `4cfc8f32` the family's own canonical program declared `ui.find` returning
`Str` and `ui.click` taking `Str`, and compiled. Slices 1 to 3 make that
program's AUTHORITY enumerable: the verb set is closed, the root is refused,
and under `taint_strict` an observed value is `Untrusted` by derivation. None
of that says anything about the target's shape, and a `Str` target means the
actuation names its target BY NAME. A name is re-resolved at every use, so:

**Nothing binds the actuation to the observation.** The evidence that
justified "this is the Approve button" is not carried anywhere. No later step
can check that the control acted on is the control that was looked at, and
design 532 §7 puts exactly that check ("identity match, expected window") on
the substrate's side of the line. The substrate cannot run it against a
string.

**Nothing expires.** A target resolved before a dialog opened is still
spellable afterwards, and the spelling still resolves, to a different
control. That is 532 §4.1's rung-1 failure direction ("the query matches a
DIFFERENT control that satisfies it, and the action succeeds on the wrong
thing") reached from rung 0, by waiting. The ladder decision bounds how a
target may be NAMED and says nothing about how long a naming stays true.

**Nothing survives a phase boundary.** This is the one with a witness outside
this item. PR #1287 (item 522) states it in its own "what is not verified":

> The postcondition check is POSITIONAL: it reports that a read follows an
> actuation in the same method, not that the read checks that actuation.
> Binding a postcondition to its step needs a target that survives a phase
> boundary, which is item 521's.

and, on the LIFO half:

> It also needs a UI target that survives a phase boundary, which is item
> 521's own slice 4: a transaction over targets re-resolved by name has a
> check-to-use race at every phase boundary.

So the missing record is not a tidiness question inside item 521. It is the
reason another item's verdict is weaker than the word it would like to use.

## 2. The record

```revl
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}
```

The list is 532 §5's, which is item 521's own: "the window or application
identity, an accessibility-tree identity where one exists, a screenshot/DOM/AX
evidence hash, the allowed action type, the user/session/task identity, a
region or coordinate bound, an expiry, and whether a confirmation is
required". Three notes on the rendering, because a field list is where a
design quietly loses an argument:

**`application` and `window` are separate.** 532 §5 writes them as one item.
An application identity alone does not distinguish two documents open side by
side, and "the wrong window of the right application" is the commonest way a
correct-looking actuation lands on the wrong thing.

**`role` may be empty, and empty is a measurement.** Not every platform
publishes an accessibility tree. An empty `role` records that none was
available; an ABSENT `role` would record nothing, and a reader cannot tell
"there was no tree" from "nobody looked".

**`action` is the target's own, not the verb's.** The verb is already on the
capability token and already on the audit surface. What the token cannot say
is that THIS target was resolved for a read and is not a target for a click.
Without the field, a target resolved for one purpose becomes a target for
another by being passed along, which is the `ui.act(target)` shape 532 §11
refuses, reached through data instead of through a verb.

`expiry` is an `Int` and every other identity field is a `Str`. The types are
registry-owned alongside the names: a target whose expiry is a string is a
target whose staleness is compared by whatever format the reader guesses.

## 3. Floor, not ceiling

The check is a FLOOR. A field the registry does not name is the author's own
and is admitted.

Stated as a decision rather than left as an omission: an extra field neither
adds nor removes authority, so refusing it would be a false refusal with no
soundness gain, while a MISSING field is a binding no later reader can
recover. The closedness argument that applies to the VERB set (532 §8: an
invented token narrows nothing and escapes every policy rule written against
the real one) does not transfer, because a field is not selectable by a
policy rule and cannot partition an authority cone.

## 4. Which half is gated, and which is not

`Untrusted[UiTarget]` is two claims with two owners, and conflating them
hides which one a program actually has.

| half | owner | gated on |
| --- | --- | --- |
| `UiTarget` | this slice | nothing |
| `Untrusted` | slice 2's derivation (`ui.find` is a source minting `screen`) | `taint_strict`, as every item-249 Slice D class is |

So an author may write `-> UiTarget` or `-> Untrusted[UiTarget]` and both
admit: the qualifier is stripped before this check runs, and under strict
mode the derivation supplies it either way. Requiring the author to WRITE the
qualifier would contradict slice 2's own argument, that sink-ness and
source-ness come from the side that grants the authority and never from the
author. The separation is measured by
`test_the_two_halves_are_separately_gated`: the canonical program admits
WITHOUT strict mode, and the target reaching the actuation refuses WITH it.

## 5. Where the check runs, and the two scope cuts

In `lower._check_ui_target_binding`, called from `_check_and_lower` right
after `_lower_type_decls`. That position is the whole reason this is not a
parser hook: the obligation is a program-level fact (a verb declared in one
place, a record declared in another) and the record table is what it is
checked against. It also runs after `taint.extract_and_normalize` has
stripped the item-249 qualifiers in place, which is what makes §4's "either
spelling" true with no second stripper.

**Externs only.** A service method's `emission[ui.find]` scope funnels through
the same `parser._capability_list` hook slice 1 uses, and deliberately does
not reach here. The obligation belongs to the declaration that actually
CROSSES; a service method declares an interface. Item 522's
`teardown_refusal` makes the same cut for the same reason, so the family has
one rule about this and not two.

**Only where the family is declared.** `UiTarget` is reserved in a program
that declares a target-carrying verb, and is an ordinary name in a program
that does not. Refusing it elsewhere would be a refusal with no authority
behind it.

## 6. Failure direction of every decision here

| decision | fails | why that is the safe side |
| --- | --- | --- |
| a target-carrying verb with no `UiTarget` in the program is refused | closed | the alternative is a verb whose target shape is whatever the author passed, which is the state this slice exists to leave |
| a missing or mis-typed registry field is refused | closed | the field is what a later phase reads; a program admitted without it produces a target no reader can use, and the refusal would arrive at run time in the substrate |
| an unknown extra field is admitted | open, deliberately | §3: an extra field carries no authority, and a false refusal here buys nothing |
| the producer's return must be the record | closed | this is the only place the record can enter the program, so admitting a `Str` return re-opens every case below it |
| an actuation must TAKE the record | closed | an actuation receiving a bare value resolves its target a second time, and a name resolved twice is two targets |
| the check is unconditional, not profile-gated | closed | a structural obligation behind a profile is an obligation most programs do not have |
| the check runs on externs and not on service methods | open, deliberately | §5: a service method is an interface, and refusing there would bound a declaration that crosses nothing |
| `UiTarget` is the author's record, not a shipped type | neither; it is a scope statement | 532 §0's argument: the effect surface is OS-specific and revl does not build the substrate |

## 7. What this does not claim

- That the fields are TRUE. `evidence` is a hash the host computed and revl
  never sees a screen. The record is a carrier with a checked shape, not an
  attestation, and 532 §7 already puts the pre-execution verification on the
  substrate's side.
- That a target is used where it was resolved. revl checks the signature, not
  the dataflow between two crossings. A program may resolve a target and act
  on a different one. **This paragraph used to end "and the taint discipline
  of slice 2 is what bounds that, not this slice". That sentence was wrong,
  and §13 has the measurement and the repair.**
- Which parameter is the target. A capability token carries no parameter
  roles (item 294's parameters narrow the capability, they do not name the
  parameters), so the check says one parameter is a `UiTarget`, not which.
  That is the same limit slice 2 recorded for the all-arguments taint
  derivation, and it is a limit rather than a choice. **§13 narrows this
  where it matters**: the capability token names no parameter, but the
  extern's SIGNATURE does, so the parameters declared `UiTarget` are the
  target positions.
- That `List[UiTarget]` will do. It is refused, because a consumer taking a
  list has not named WHICH target it acts on, which is the bare-string defect
  one level out.

## 8. Self-host

Does this need a self-host port? **The question is older than this slice, and
the measurement moves its date.**

532 §9 records that `admit_src` issues no admission (item 417), so a program
carrying a reserved UI token cannot be ADMITTED by the self-host gate, and
that the porting obligation fires "when a slice of this item starts deciding
admission on a UI token" (which §9 places at slice 3). Both halves were read
rather than measured. Measured here, through the harness
`tests/test_selfhost_lower.py` uses:

| program | reference | `admit_src` |
| --- | --- | --- |
| `emission[ui]` (the root alone) | refused, G8 | `""` |
| `emission[ui.drag]` (an undeclared verb) | refused, G8 | `""` |
| `emission[ui.click.pixel]` (a rung) | refused, G8 | `""` |
| the canonical family | admitted | `""` |

All three of those refusals are SLICE 1's. So the obligation fired at slice 1,
not at slice 3, and has been outstanding since. `""` is NO OBJECTION, which is
agreement by silence, and §9 names agreement by silence as the exact thing the
marker exists to prevent.

What this is and is not. It is not a false ADMISSION: the native gate's
verdict vocabulary has no `admitted` arm (`gate.Verdict.kind`), so `""` from
the self-host means the reference must be asked, and the gate is fail-closed
in the sense §9 claims. It IS a gap nothing reports. The self-host
differential oracles compare verdicts over a corpus whose refusals are
in-slice; a G8 refusal classifies OUT-OF-SLICE; so every oracle stays green
over a family of checks the self-host does not run. That is item 391's own
finding, restated by 417, arriving here.

**Repaired in slice 3, by a named decline rather than by a port.**
`tools/build_gate_crate.py`'s frontier table is the mechanism §9 is asking
for: a construct in it makes the native gate answer
`OutsideFrontier { reason }` instead of no objection. It had two axes,
`keywords` and `builtins`, both derived from the reference compiler's own
tables. A third, `capability_roots`, is derived from `ui_family.ROOTS` minus
whatever `selfhost/lower.rvl::reserved_capability_root` names, which is
nothing today and empties the table in the same wave as a future port.
`src/revl/ui_family.py` joins `DIGEST_INPUTS` for the reason
`src/revl/lexer.py` is already there: a root added to it moves the covered
surface, and a `frontier_id` that did not move would claim two gates cover the
same surface when they do not. Both builders were regenerated and
`tests/test_gate_crate_admit.py` drives the result from the consumer side, so
a source carrying `ui.` or `screen.` now comes back `outside_frontier` with
code `FRONTIER`.

What that discharges and what it does not. The gate DECLINES BY NAME instead
of agreeing by silence, which is §9's requirement. It is not a port:
`selfhost/lower.rvl` still runs none of item 521's checks, so
`test_the_selfhost_gate_does_not_decide_a_ui_program` keeps measuring the
`admit_src` silence and fails by name if the self-host ever grows a verdict
there. The frontier guard is lexical and conservative: a source mentioning the
namespace in a host body or a comment costs a false `OutsideFrontier`, which
is the only direction this crate is allowed to err in, and no program in
`examples/`, `stdlib/` or `selfhost/` mentions either root today.

## 9. `retention.persistence_sink_of`, and why `ui` does not join it

The other question PR #1284 left open: `ui.download` does put a file on the
host, which is the shape `retention.PERSISTENCE_SINK_SCOPES` describes, so
should `ui` join that set?

**Measured answer: no, not as the set stands.** `persistence_sink_of` reads a
token's dotted HEAD, by construction and by its own docstring ("only the
FIRST capability is consulted for its head"). The head of four of the five
verbs is `ui`. Measured by compiling the same program with one capability
changed, under an expired `retention` policy:

| crossing | today | with `ui` in the set |
| --- | --- | --- |
| `db.write` (control) | refused | refused |
| `ui.download` | admitted | refused |
| `ui.click` | admitted | **refused** |
| `ui.find` | admitted | **refused** |
| `screen.observe` (control) | admitted | admitted |

Three refusals for one true one. `ui.click` stores nothing, and a
`Retained[T, P]` value reaching it is not past-deadline data written to a
store. That is not a conservative widening, it is a wrong classification that
happens to contain a right one.

This is the same head-granularity problem 532 §5.1 solved for the taint roles,
and it has the same answer: a per-verb registry entry, the shape
`ui_family.TAINT_ROLES` has, which `retention.persistence_sink_of` would
consult for a reserved token the way `taint._sink_of` already does. That is a
change to item 472's module with its own failure direction and its own
measurement of item 472's surface, and it is not item 521's to make on the way
past. Left alone rather than widened on a hunch, which is where PR #1284 left
it; what is added here is the reason, with numbers.

## 10. What slice 5 needs from this

532 §10's slice 5 is "one recorded action carrying application, window, target
role, target name, target evidence hash, action, capability, session". Seven
of those eight are `TARGET_FIELDS` entries by the same names. The eighth,
`capability`, is the declared token and comes from the declaration rather than
from the target. So the receipt is (the target's binding) plus (the crossing's
declared token), and the reason that is worth saying out loud is that it means
slice 5 invents no vocabulary: a receipt member that the target does not carry
and the declaration does not name would be a member nothing can fill.

## 11. What is not verified about slice 4

- **No step executes.** No `@py` body in the oracle is ever run. revl checks
  the signature and the record; a target is resolved by a substrate this
  repository does not build (532 §7, item 539).
- **The fields are not validated at run time.** An `expiry` in the past, an
  `evidence` that is not a hash, an `action` that names no verb: all
  admitted. The obligation is that the field EXISTS, and a value check has no
  home in a compiler that never sees a screen.
- **The dataflow between resolve and actuate is unchecked** (see §7). Closed in part by §13; what remains open there is stated in §13.4.
- **The backends** were not exercised. No emitted `@py`, `@ts`, `@rs`, `@go`,
  `@java` or wasm body for a UI verb was run, here or anywhere.
- **Multi-file programs** were not measured. The record is looked up in the
  program's own type table, which is what `compile_files` merges into, but no
  fixture declares `UiTarget` in one file and the family in another.

## 12. The recorded action (slice 5)

`src/revl/ui_action.py`. The gap it closes, stated as the asymmetry rather
than as a feature: slice 1 put a UI verb on the G8 audit surface, so `revl
audit` says a component CAN reach `ui.click`, and nothing anywhere says what
was clicked. Every other revl step has an answer. An effect carries a WAL
record with an inverse, an emission carries a deferred record and a flush
proof, a boundary crossing carries a receipt, and a model completion carries
item 517's decision object. A computer-use step carried nothing.

### 12.1 What the receipt carries, and why it is the whole binding

532 §10 asks for eight members: application, window, target role, target name,
target evidence hash, action, capability, session. All eight are here. The
receipt carries the REST of the target binding too (`bounds`, `expiry`,
`confirm`) plus the crossing key, and that is a decision rather than a
widening.

A receipt carrying a SUBSET of the target's binding recreates, at audit time,
exactly the "which field got dropped" question slice 4 removed at compile
time, and it recreates it at the moment somebody is looking. `expiry` is the
sharpest case: it exists so staleness is noticeable, and a receipt that omits
it makes the staleness unnoticeable again in the one artifact an auditor
reads.

The crossing key is item 517's pair (`component`, `step_index`), reused rather
than re-invented, so a UI receipt and a model decision index a component's
steps the same way. A receipt with no crossing key indexes to nothing and
cannot be lined up against the audit surface that named the reach.

The target half of the member list is READ from `ui_family.TARGET_FIELDS`
rather than copied. A field added to the binding is a member of the receipt in
the same change, which is the only way two tables in two modules stay in step.

### 12.2 The one rule

**Every outcome carries every member.** Four arms, none of which may thin the
record. That is 532 §10's named oracle for this slice, which is item 525's
"refusals as legibly as successes" applied to one step.

| arm | what it records |
| --- | --- |
| `performed` | the substrate performed the step. It says nothing about whether the control did what the author expected: that is a postcondition, and postconditions are item 522's |
| `refused` | something refused the step before it ran: an approval rule, a capability bound, a budget or deadline |
| `unresolved` | the target did not bind |
| `failed` | the step ran and the substrate reported it did not complete |

`unresolved` is the arm a thinning producer would drop first and the one that
matters most. 532 §4.2 refuses the ladder's descent inside one call, so an
`unresolved` receipt is the POSITIVE evidence that a `ui.click` which could
not bind its target did not become a `ui.click.pixel`. Its members are all
fillable: `role`, `name` and `action` record what was SOUGHT, and `evidence`
records the observation that was searched. A producer that thinned it would be
dropping facts it had.

`unresolved` and `failed` are separate because a target that bound and then
failed is a different fact from one that never bound, and the residue a UI
transaction must assume differs between them.

### 12.3 Present is not enough

`verify` refuses three things a presence check admits, and each is a record
that looks complete and is not.

1. **An empty member.** A member filled with `""` passes "is it there" and
   answers nothing. The one exception is `role`, and it is an exception for
   slice 4's own reason: not every platform publishes an accessibility tree,
   `""` says none was available, and an ABSENT `role` would leave a reader
   unable to tell "there was no tree" from "nobody looked". `EMPTY_ALLOWED` is
   that one name and a test pins that it is the only one.
2. **An outcome outside the closed arm set.** An arm nobody declared is an arm
   no consumer branches on.
3. **A member nobody named.** A producer adding one has an extra fact that
   reaches no consumer while looking as though it did.

Point 3 is the OPPOSITE call from slice 4's record check, which admits an
unknown field (§3), and the two differ because they bound different things: a
record is the author's own data structure, and a receipt is a wire shape
between a substrate and an auditor. Saying so here is what stops the next
reader from calling one of them a mistake.

The verdict collects every reason rather than stopping at the first, because a
caller repairing a record wants the whole list; one member per attempt turns a
malformed record into a sequence of round trips during which the operator
learns the shape one field at a time.

### 12.4 Not signed, deliberately

Item 517's model decision carries a MAC. This does not, and the absence is
pinned by `test_no_member_claims_authenticity` so a later slice cannot add one
without arguing for it.

517 signs because the producer (the model host) and the consumer (the
promoter) are both inside revl's world. A UI step is performed by the
computer-use substrate, which 532 §7 and item 539 put outside this repository:
revl builds no key for it and holds none. A `signature` member revl defined
and nobody could fill would read as an attestation and would not be one, which
is worse than no field at all. What this module bounds is COMPLETENESS, which
is a property of the record rather than of the recorder.

### 12.5 What is not verified about slice 5

- **Nothing produces a receipt.** The module defines the shape and checks it;
  no revl surface emits one, because the thing that performs a UI step is the
  substrate. That is the same position item 517's module was in when it landed
  (`revl.model_evidence` was a vocabulary and a `verify` before
  `shadow_promotion` consumed it), and the same reason: the shape has to exist
  before a consumer can require it.
- **The fields are not checked for truth.** `evidence` is a hash the host
  computed; revl never sees a screen. `verify` checks presence, type,
  inhabitation and vocabulary.
- **Nothing lines a receipt up against a compiled program.** A receipt naming
  a `capability` the component's reach does not contain is admitted here. That
  cross-check is possible (the reach is on the G8 audit surface) and is not
  done, because the consumer that would run it does not exist yet.

## 13. Where an actuated target came from (issue #1371)

§7's second bullet named slice 2's taint discipline as what bounds a program
that resolves one target and acts on another. Measured on `fc0d84ce`, that
bound runs the other way.

### 13.1 The measurement

`ui.find` is a taint SOURCE and `ui.click` is an all-arguments SINK, so under
`taint_strict`:

| program | verdict |
| --- | --- |
| resolve a target, then click the target resolved | refused, G9 |
| click a `UiTarget` record literal written in the body | admitted |

and the second admits under every profile. A forged target carries no origin,
so there is nothing on it to refuse: taint bounds what a value is DERIVED
FROM, and it cannot bound a value derived from nothing. A value derived from
nothing is exactly the one no observation justifies. The discipline named as
the bound refuses the honest program and admits the forged one.

Nine more spellings were admitted on the same tree, including a `pure` extern
minting the record, a helper `fn` returning one, a mutable binding rebound
from a resolution to a literal, and a SECOND COMPONENT building the target and
handing it over a service. All ten are in
`tests/test_ui_target_provenance_1371.py`.

### 13.2 The invariant, and why it is placed on the construction

**In an admitted program, every `UiTarget` originates in a target-producing
crossing.** `lower._check_ui_target_provenance` holds it with three refusals,
all G8 like slice 4's own three:

| origin | what the program did | fails |
| --- | --- | --- |
| `constructed` | a record literal carrying the declared target's fields | closed |
| `minted` | an extern returning the record without declaring `emission[ui.find]` | closed |
| `rebound` | a functional update rewriting a registry-owned field of a resolved target | closed |

What is refused is the CONSTRUCTION, not the flow to a particular use, and
that is the whole of the placement argument. A check on the flow is only as
good as its walk, and issue #1327 measured what that costs on this exact
family: a walk that named three statement kinds missed seven spellings of the
same crossing, every miss in the fail-open direction. A target that was never
constructed came from a crossing, whatever path it then took, so there is no
position for a walk to miss.

An extra field the registry does not name is the author's own (§3's floor) and
may still be updated. The ten registry fields are ONE binding: an update of
`name` keeps the resolved control's evidence hash and points the actuation at
a different control, which is the race written in one expression.

### 13.3 Which parameter is the target, narrowed

§7's third bullet is about the capability TOKEN, and it stands: a token names
no parameter. The extern's SIGNATURE does. Slice 4 already requires an
actuation to declare a `UiTarget` parameter, so the positions declared with
that type are the target positions, and this check reads them there rather
than guessing. That is narrower than "one parameter is a `UiTarget`" and it is
as far as the declaration can be read.

### 13.4 What is still not claimed

- **Not that the target is the one resolved for THIS step.** A program that
  resolves two targets and acts on the second acted on a target it resolved,
  and it admits. The stronger form is a resolved HANDLE a phase boundary
  carries, which needs the substrate that actually resolves a target
  (item 539, upstream `inso1337/revl-harness#11`).
- **Not that the resolution is still fresh.** `expiry` is a field, not a
  check; revl never sees a clock a screen agrees with.
- **A `config` field typed `UiTarget` is admitted, deliberately.** It is the
  one entry point left open, and the failure direction is OPEN. An operator
  supplying a target through the environment contract (item 350) is the same
  authority that granted the component `ui.click`, and it is not the program
  writing authority for itself. It is also load-bearing today: an activation
  body cannot bind an emission result (`let t = emit …` there is a G6
  refusal), and an activation body is the only place an `Approval[C]` can be
  minted, so a CONFIRMABLE computer-use crossing can only act on a target that
  entered the component some other way. Refusing a `UiTarget` config field
  would make a confirmable UI crossing unwritable. That is a fact about the
  system worth recording on its own.
- **Nothing crosses a compilation unit.** The closure holds within one
  compiled program, including across components. A target arriving over a
  remote seam or from a separately compiled unit is not traced.

### 13.5 Self-host

**No port.** §8's answer covers this refusal without extension: the frontier
axis `capability_roots`, derived from `ui_family.ROOTS`, already makes the
native gate answer `OutsideFrontier { FRONTIER }` for any source carrying
`ui.` or `screen.`, so a fourth reference refusal over the same namespace adds
no agreement obligation. There is no tag and no message for the two sides to
disagree about, because the self-host side issues neither.
`test_the_selfhost_gate_still_declines_the_namespace_by_name` keeps measuring
the `admit_src` silence on the new refusal specifically and fails by name the
day it stops, at which point the port and the message agreement become real
work. `src/revl/ui_family.py` is a `DIGEST_INPUTS` entry, so the crate and the
wasm crate were both regenerated for this change.

### 13.6 Why not the transaction unit

`ui_transaction` already records `targetResolvedBy` per actuating step
(item 522 slice 5), and it is `null` for a forged click and `"ui_find"` for an
honest one. The report can see the difference and issues no verdict on it. It
cannot simply become one: `targetResolvedBy` is `null` for an honest target
passed through a local `fn` too, so a refusal keyed on it would refuse correct
programs. That is the measured reason this lands in the type and capability
layer, and it is why nothing in `ui_transaction.py` changed.
