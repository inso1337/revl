# Design: the computer-use target binding

Design-doc id 565. Roadmap item served: 521 (issue #1195), slice 4 of the
plan in `docs/design/532-typed-computer-use.md` §10. 532 stays the item's
design note; this one exists because slice 4 is the first slice that decides
anything about a computer-use program's DATA rather than its authority, and
532 §5 records the binding in one sentence and then moves on.

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
  on a different one, and the taint discipline of slice 2 is what bounds that,
  not this slice.
- Which parameter is the target. A capability token carries no parameter
  roles (item 294's parameters narrow the capability, they do not name the
  parameters), so the check says one parameter is a `UiTarget`, not which.
  That is the same limit slice 2 recorded for the all-arguments taint
  derivation, and it is a limit rather than a choice.
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

Not repaired in this slice, and the reason is scope rather than difficulty.
The repair is the NAMED DECLINE §9 asks for, and the mechanism already exists:
`tools/build_gate_crate.py`'s frontier table, which makes the native gate
answer `OutsideFrontier { reason }` for a construct the self-host does not
cover. Today that table has two axes, `keywords` and `builtins`, both derived
from the reference compiler's own tables; a reserved-capability-root axis
derived from `ui_family.ROOTS` is the third. It carries a crate regeneration
(both `build_gate_crate.py` and `build_gate_wasm.py`, then
`tests/test_gate_crate_admit.py`, which is the only gate that actually shells
cargo), and §9 ties that regeneration to slice 3, which carries one anyway for
the prefix-closure check. Doing it here would put the expensive half of slice
3 in slice 4 and leave slice 3 with the cheap half.

The measurement is pinned by
`test_the_selfhost_gate_does_not_decide_a_ui_program`, which fails by name if
the self-host ever grows a verdict on these programs, so the next reader of
§9 finds the date corrected rather than re-derives it.

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

## 11. What is not verified

- **No step executes.** No `@py` body in the oracle is ever run. revl checks
  the signature and the record; a target is resolved by a substrate this
  repository does not build (532 §7, item 539).
- **The fields are not validated at run time.** An `expiry` in the past, an
  `evidence` that is not a hash, an `action` that names no verb: all
  admitted. The obligation is that the field EXISTS, and a value check has no
  home in a compiler that never sees a screen.
- **The dataflow between resolve and actuate is unchecked** (see §7).
- **The backends** were not exercised. No emitted `@py`, `@ts`, `@rs`, `@go`,
  `@java` or wasm body for a UI verb was run, here or anywhere.
- **Multi-file programs** were not measured. The record is looked up in the
  program's own type table, which is what `compile_files` merges into, but no
  fixture declares `UiTarget` in one file and the family in another.
