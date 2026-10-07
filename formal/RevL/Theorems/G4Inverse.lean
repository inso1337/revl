/-!
# G4Inverse — the `INV` differential-oracle row (issue #2097)

The rule: the `undo` a bracket declares must be the INVERSE its acquisition
owns. The checker states it in two places — `src/revl/lower.py`'s
`_check_site_release` (issue #1859: a host acquisition's family release on the
handle it bound, and an `extern acquire`'s declared inverse on that handle) and
`_method_effect_inverse` (issue #1945: a host write's inverse on the same
receiver and the same key). Both raise code `G4` with category `inverse`, and
four corpus documents that exercise them —

* `examples/rejections/g4_extern_undo_not_declared.rvl`,
* `examples/rejections/g4_undo_not_release.rvl`,
* `examples/rejections/g4_method_write_not_inverse.rvl`,
* `tests/fixtures/canary_candidate_inverse.rvl` —

were checked by the shipped checker and then held in the
`out-of-fragment-inverse` ledger bucket, because nothing in `formal/` decided
them. This module is the model row that empties that bucket.

## The row, and the route it takes

The precedent for the shape is `RevL.G4Deferred` (issue #1742) and
`RevL.G4Approval` (issue #1455), both of which left the same ledger when their
rows landed. The route taken here is the one `RevL.G9Flow` (issue #1811 group
2) and `RevL.GRetain` (issue #1811 group 3) took: the exporter
(`formal/harness/diff_corpus.py`, `g4inverse_rows`) runs the real checker and
reads five facts out of the refusal it raises:

* the **arm** the refusal belongs to — which of the checker's three sentences
  the message is (the family release, the declared extern inverse, the host
  write inverse). The checker spells the three differently and the exporter
  matches each sentence exactly; a `G4`/`inverse` refusal whose sentence the
  row does not carry produces no row at all and a `g4inverse coverage` finding,
  never a guessed arm;
* the **acquisition** — `Map.new`, `log_open`, `store.insert` — as the
  message's own `` the `undo` of `let store = effect Map.insert(...)` `` head
  spells it;
* the **demanded inverse verb** — `drop`, `log_close`, `remove` — and the
  **demanded spelling** — `store.drop()`, `log_close(log)`,
  `store.remove(k)` — both from the checker's own `` write `undo …` `` advice;
* the **site's `undo`**, spelled the way the checker's `_source_spelling`
  spells it, so the row's `site` column is directly comparable with the
  `inv` column the checker printed.

The row then decides: does the acquisition's inverse — the table entry for a
host family, the declared callee for an `extern acquire` — equal the verb the
checker demanded, AND is the site's own `undo` the demanded spelling? A
refusal is a `fail` (the rule is VIOLATED at that site), and `fail` is what the
harness files under `agree-G4`. An `ok` on a modelled refusal would be the
fatal `missed-G4`.

## What this row is NOT: the rule on the corpus, not coverage of the walk

This is the distinction issue #2097 requires be stated, and it is the whole
reason the row is a fallback rather than the fix.

**What the row decides.** The rule, at the acquisition the checker discovered,
with the inverse the checker demanded, against the site's `undo` the checker
itself read. If the checker's own comparison is what the rule is about, then
this row agrees with it by construction: the row's premises ARE the checker's
report.

**What the row does not witness.** That the checker's WALK is complete — that
it looks at every bracket, every `extern acquire` and every host write. A
checker that stopped checking one of the three arms exports no row for it, and
this row cannot tell the difference between "the inverse is right here" and
"the checker did not look here". Roadmap item 418 step 9 is deliberately not
claimed here or anywhere in this row.

**Why the row is still not vacuous.** An absent row is a FAILURE, not a pass.
`hostRelease` and `writeInverse` are `none` for an acquisition this module does
not name — a host family or a host write the checker's tables have grown —
and the exporter emits no row for such a refusal; `diff_corpus.py` files every
modelled `G4`/`inverse` refusal with no row under `missed-G4`, which is in
`FATAL_BUCKETS`. So a checker that began refusing a new host family, or that
stopped naming the inverse it demands, turns this row RED rather than silently
green — which is exactly the direction the coverage route would make
unnecessary and this route cannot. (The extern arm has no table at all: its
requirement IS the acquisition's own declaration, so `requiredInverse .extern`
is never `none`, and the arm's content is the site comparison.)

**The non-vacuity flip is executed, not asserted.** `formal/harness/diff_corpus.py`'s
`g4inverse_coverage` re-decides every exported row with its own `site` column
replaced by the checker's demanded spelling, and requires the verdict to FLIP:
refused at the site the checker reported, admitted at the spelling the checker
demanded. A row that returned a constant, or that ignored the receiver or the
key, fails the gate. The demanded spelling is read out of the checker's own
refusal, never invented harness-side.

## The tables are stated HERE, not imported

`hostRelease` is `lower._HOST_ACQUIRE_VERBS` (`typecheck.py`) read through
`lower._host_release_of`, and `writeInverse` is `lower._HOST_WRITE_INVERSE` as
`lower._host_write` reads it. Both are restated in this module rather than
imported from `src/revl/`, so that widening the checker's tables moves the
checker alone and the refusal becomes the harness's fatal `missed-G4` instead
of a silent agreement. `kindOfString` is the same discipline applied to the
three refusal sentences the exporter classifies.

This module lives in `RevL.Theorems` (L2) and imports nothing but the kernel, so
it does not collide with `RevL.G9Flow` or `RevL.GRetain` (also L2); the import
layering (`formal/scripts/layering_gate.py`) forbids one L2 file importing
another.
-/

namespace RevL.G4Inverse

/-! ## The three arms, and the tables they read

The checker's three sentences are three different judgments with three
different sources of truth: a host family's release (a table), an
`extern acquire`'s declared inverse (a declaration), and a host write's
inverse (a table). They are kept apart rather than merged because a merged row
could not say which table it read, and the harness could not tell a misread arm
from a widened table.
-/

/-- Which of the checker's three `G4`/`inverse` sentences a refusal is:
`lower._check_host_release` (the family release), `lower._check_extern_release`
(the declared inverse) and `lower._method_effect_inverse` (the write inverse).
The three are distinguished by the checker's own wording, not by a guess. -/
inductive InvKind where
  | host | extern | write
  deriving DecidableEq, Repr

/-- The arm a checker refusal's own sentence names, or `none` for a sentence
this row does not carry. `none` means no row is emitted and the harness records
a `g4inverse coverage` finding — a gate failure — rather than choosing an arm. -/
def kindOfString : String → Option InvKind
  | "host" => some .host
  | "extern" => some .extern
  | "write" => some .write
  | _ => none

/-- The host families with exactly one release, as `lower._HOST_ACQUIRE_VERBS`
(`typecheck.py`) holds them and `lower._host_release_of` reads them. An
acquisition this table does not name has no release and therefore no row:
`requiredInverse` is `none`, the exporter emits nothing, and the harness files
the refusal under the fatal `missed-G4` rather than reading it as an agreement.

The three are exactly the checker's: `Map.new` -> `drop`, `Pool.open` ->
`close`, `Stream.source` -> `close`. -/
def hostRelease : String → Option String
  | "Map.new" => some "drop"
  | "Pool.open" => some "close"
  | "Stream.source" => some "close"
  | _ => none

/-- The host writes with exactly one inverse, as `lower._HOST_WRITE_INVERSE`
holds them and `lower._host_write` reads them, keyed `<family>.<verb>`. An
acquisition this table does not name is not a write this rule speaks about, so
`requiredInverse` is `none` and the refusal — if the checker raises one — is the
fatal `missed-G4`.

The three are exactly the checker's: `Map.insert` -> `remove`,
`Map.insert_if_absent` -> `remove`, `Map.remove` -> `insert`. -/
def writeInverse : String → Option String
  | "Map.insert" => some "remove"
  | "Map.insert_if_absent" => some "remove"
  | "Map.remove" => some "insert"
  | _ => none

/-- The inverse the acquisition OWNS, in the verb the checker's own advice
spells: the family release for a host acquisition, the write's table entry for
a host write, and the DECLARED callee for an `extern acquire`.

The extern arm is `some verb` and not a table lookup, and that is the honest
reading rather than a convenience: the checker's `_check_extern_release` takes
`inv` from `env.extern_inverse` — the acquisition's own `undo` DECLARATION —
so there is no table to consult, and the only fact the arm carries is the verb
the checker's advice named. `extern_arm_is_the_declared_inverse` below states
it. -/
def requiredInverse (kind : InvKind) (acq verb : String) : Option String :=
  match kind with
  | .host => hostRelease acq
  | .write => writeInverse acq
  | .extern => some verb

/-- The rule at one site, decided. `acq` is the acquisition, `verb` the inverse
verb the checker demanded, `inv` the demanded spelling and `site` the `undo`
the checker read at the site. The rule HOLDS when the acquisition's own inverse
is the verb the checker demanded AND the site's `undo` IS the demanded
spelling — spelling equality is what carries the receiver and the key, which is
the whole of issue #1945's premise and issue #1859's "on THAT handle".

`false` for an acquisition the row does not model, so an unmodelled refusal can
never be read as an agreement. -/
def rowB (kind : InvKind) (acq verb inv site : String) : Bool :=
  match requiredInverse kind acq verb with
  | none => false
  | some r => decide (r = verb) && decide (site = inv)

/-- The rule as a proposition, and `rowB` decides it. This is the bridge the
oracle's `#print axioms` gate rests on: the decider the harness prints is this
one, and it is pinned to the rule rather than to a chosen computation.

The RHS is the rule: the acquisition's OWN inverse is the verb the checker
demanded, and the site's `undo` is the spelling the checker demanded. -/
theorem rowB_iff (kind : InvKind) (acq verb inv site : String) :
    rowB kind acq verb inv site = true ↔
      requiredInverse kind acq verb = some verb ∧ site = inv := by
  unfold rowB
  cases requiredInverse kind acq verb with
  | none => simp
  | some r => simp

/-! ## The tables, and what falls off them

`requiredInverse` is the rule's one lookup, and its `none` is the direction
that keeps the row from passing by looking away: an acquisition the tables do
not carry has no verdict at all, and the harness's `missed-G4` is fatal.
-/

/-- `requiredInverse` is `none` exactly off the two tables: a host acquisition
outside `hostRelease`, or a write outside `writeInverse`. The extern arm has no
table — its requirement is the acquisition's own declaration — so it is never
`none`. -/
theorem requiredInverse_none_off_the_tables (kind : InvKind) (acq verb : String) :
    requiredInverse kind acq verb = none ↔
      (kind = .host ∧ hostRelease acq = none)
        ∨ (kind = .write ∧ writeInverse acq = none) := by
  cases kind <;> simp [requiredInverse]

/-- The extern arm's requirement is the DECLARATION's, read out of the
checker's refusal: `requiredInverse .extern` is the verb the checker named, so
that arm's whole content is the site comparison. That is the honest reading of
`lower._check_extern_release` — `inv` comes from `env.extern_inverse`, not from
a table — and it is why this arm has no table of its own and why the row's
`acq` column is carried for the record rather than looked up there. -/
theorem extern_arm_is_the_declared_inverse (acq verb : String) :
    requiredInverse .extern acq verb = some verb := rfl

/-- The host release table is the checker's, all three entries. -/
theorem host_release_table :
    hostRelease "Map.new" = some "drop"
      ∧ hostRelease "Pool.open" = some "close"
      ∧ hostRelease "Stream.source" = some "close" := by
  refine ⟨?_, ?_, ?_⟩ <;> rfl

/-- The write inverse table is the checker's, all three entries. -/
theorem write_inverse_table :
    writeInverse "Map.insert" = some "remove"
      ∧ writeInverse "Map.insert_if_absent" = some "remove"
      ∧ writeInverse "Map.remove" = some "insert" := by
  refine ⟨?_, ?_, ?_⟩ <;> rfl

/-- The three arms the exporter classifies are the three the row carries. -/
theorem kindOfString_is_the_kind :
    kindOfString "host" = some .host
      ∧ kindOfString "extern" = some .extern
      ∧ kindOfString "write" = some .write := by
  refine ⟨?_, ?_, ?_⟩ <;> rfl

/-- A host family outside the checker's three has NO release, so the exporter
emits no row and the harness files the refusal under the fatal `missed-G4`.
This is the direction that makes widening the checker's family set a red row
rather than a green one. -/
theorem unmodelled_acquisition_has_no_inverse :
    hostRelease "Log.open" = none ∧ writeInverse "Map.get" = none := by
  refine ⟨?_, ?_⟩ <;> rfl

/-- The extern arm has no table, so it can never fall to `none` and be silently
skipped: `requiredInverse .extern` is always `some`, whatever the acquisition
and the verb. That is the same fact as
`extern_arm_is_the_declared_inverse`, stated in the form the boundary is used
in — the arm's content is the site comparison, and the acquisition is carried
for the record. -/
theorem extern_arm_never_falls_off_a_table (acq verb : String) :
    requiredInverse .extern acq verb ≠ none := by
  simp [requiredInverse]

/-! ## The corpus shape

The four documents issue #2097 names, as the exporter reads them: the arm the
checker's sentence belongs to, the acquisition it named, the inverse verb and
spelling its advice demanded, and the `undo` it read at the site. Every one of
these is the checker's; the module chooses none of them.

`corpusExternSite`, `corpusHostSite` and `corpusWriteSite` are the three
DISTINCT site spellings the four documents carry — the extern one calls a
different function entirely, the host one reads a different verb, and the write
one passes a different key. All three are `fail`s, which is the whole content of
the bucket this row empties.
-/

/-- `examples/rejections/g4_extern_undo_not_declared.rvl`: the acquisition. -/
def corpusExternAcq : String := "log_open"

/-- The declared inverse's verb, from the checker's own advice. -/
def corpusExternVerb : String := "log_close"

/-- The demanded spelling, verbatim from `` write `undo log_close(log)` ``. -/
def corpusExternInv : String := "log_close(log)"

/-- The site's `undo`, as the checker's `_source_spelling` spells it. -/
def corpusExternSite : String := "log_flush()"

/-- `examples/rejections/g4_undo_not_release.rvl` and
`tests/fixtures/canary_candidate_inverse.rvl`: the host acquisition. -/
def corpusHostAcq : String := "Map.new"

/-- The family's release, from the checker's hint's own table. -/
def corpusHostVerb : String := "drop"

/-- The demanded spelling, verbatim from `` write `undo store.drop()` ``. -/
def corpusHostInv : String := "store.drop()"

/-- The site's `undo` in `g4_undo_not_release.rvl`: a READ of the same handle,
not its release. -/
def corpusHostSite : String := "store.get(\"x\")"

/-- `examples/rejections/g4_method_write_not_inverse.rvl` and the canary: the
host write. -/
def corpusWriteAcq : String := "Map.insert"

/-- The write's inverse verb, from the checker's hint's own table. -/
def corpusWriteVerb : String := "remove"

/-- The demanded spelling, verbatim from `` write `undo store.remove(k)` ``. -/
def corpusWriteInv : String := "store.remove(k)"

/-- The site's `undo` in `g4_method_write_not_inverse.rvl`: the right verb on
the right receiver with the WRONG key. -/
def corpusWriteSite : String := "store.remove(\"not-the-key\")"

/-- The site's `undo` in `tests/fixtures/canary_candidate_inverse.rvl`: the
same shape, a different wrong key. -/
def corpusCanarySite : String := "store.remove(\"some-other-key\")"

/-- **The four corpus rows are REFUSED.** Each document's own columns, decided:
the extern declaration's inverse `log_close` against a site `log_flush()`, the
host family's release `drop` against a site `store.get("x")`, and `Map.insert`'s
inverse `remove` against two sites that pass the wrong key. Four `false`s, which
is the whole content of the bucket this row empties. -/
theorem corpus_rows_refused :
    rowB .extern corpusExternAcq corpusExternVerb corpusExternInv
        corpusExternSite = false
      ∧ rowB .host corpusHostAcq corpusHostVerb corpusHostInv
          corpusHostSite = false
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          corpusWriteSite = false
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          corpusCanarySite = false := by
  refine ⟨?_, ?_, ?_, ?_⟩ <;> decide

/-- **Each corpus row is ADMITTED at the spelling the checker demanded** —
nothing but the `site` column moved. This is the flip `g4inverse_coverage`
re-decides for every exported row, and it is what makes the four `false`s
above a verdict about the site rather than a constant. -/
theorem corpus_rows_flip_at_the_demanded_spelling :
    rowB .extern corpusExternAcq corpusExternVerb corpusExternInv
        corpusExternInv = true
      ∧ rowB .host corpusHostAcq corpusHostVerb corpusHostInv corpusHostInv = true
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          corpusWriteInv = true := by
  refine ⟨?_, ?_, ?_⟩ <;> decide

/-- **The host arm reads its TABLE**: the same site spelling against a demand
that has left `hostRelease` is refused, so the row is not merely comparing the
checker's advice with itself. A checker that began refusing under a family
outside `_HOST_ACQUIRE_VERBS` turns this row RED rather than green. -/
theorem corpus_host_row_flips_when_the_demand_leaves_the_table :
    rowB .host corpusHostAcq "close" "store.close()" "store.close()" = false := by
  decide

/-- **The write arm reads its RECEIVER**: issue #1859's "on THAT handle" is
carried by the spelling, so the demanded verb and key on a SIBLING handle is
still refused. A row that compared verbs and keys while ignoring the handle
would return `true` here. -/
theorem corpus_write_row_flips_when_the_receiver_moves :
    rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
        "other.remove(k)" = false := by
  decide

/-- The verb the row compares against is the one the tables hold, at all three
arms: the host family's release, the write's entry, and the extern's declared
callee. -/
theorem corpus_row_verbs_are_the_tables :
    requiredInverse .host corpusHostAcq corpusHostVerb = some corpusHostVerb
      ∧ requiredInverse .write corpusWriteAcq corpusWriteVerb
          = some corpusWriteVerb
      ∧ requiredInverse .extern corpusExternAcq corpusExternVerb
          = some corpusExternVerb := by
  refine ⟨?_, ?_, ?_⟩ <;> rfl

/-- **Non-vacuity, the executed flip.** The four corpus shapes, each refused at
the site the checker reported and admitted at the spelling the checker
demanded, plus the two directions that leave the tables — a demand off
`hostRelease`, and a sibling receiver. A row that returned a constant, that
ignored the site's receiver or key, or that read no table, fails here. This is
the theorem `g4inverse_coverage`'s ratchet mirrors, and the one the harness's
site-substitution re-decision executes. -/
theorem g4_inverse_not_vacuous :
    rowB .host corpusHostAcq corpusHostVerb corpusHostInv
        corpusHostSite = false
      ∧ rowB .host corpusHostAcq corpusHostVerb corpusHostInv corpusHostInv = true
      ∧ rowB .host corpusHostAcq "close" "store.close()" "store.close()" = false
      ∧ rowB .extern corpusExternAcq corpusExternVerb corpusExternInv
          corpusExternSite = false
      ∧ rowB .extern corpusExternAcq corpusExternVerb corpusExternInv
          corpusExternInv = true
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          corpusWriteSite = false
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          corpusWriteInv = true
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          corpusCanarySite = false
      ∧ rowB .write corpusWriteAcq corpusWriteVerb corpusWriteInv
          "other.remove(k)" = false := by
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_⟩ <;> decide

end RevL.G4Inverse
