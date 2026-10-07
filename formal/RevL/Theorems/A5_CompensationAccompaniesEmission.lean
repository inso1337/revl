/-!
# A5: compensation accompanies an emission

Issue #2114. `formal/STATUS.md` carried A5 at `none` / 0 rows with "Unbuilt
work" in Notes, on the strength of `docs/rejections.md`'s claim that the
guarantee is enforced BY CONSTRUCTION: `compensate` is an OPTIONAL slot
(`DESIGN.md`, "an `emission` **may** declare a `compensate` clause"), and a
guarantee whose subject is an optional clause is vacuous until something
states when it becomes required.

Something does. `src/revl/ui_family.py::teardown_refusal`, called from
`src/revl/parser.py` at extern-declaration time over the declared
`capabilities`, states the obligation the moment the emission is a
COMPUTER-USE one, and the `REVERSIBILITY` registry is the table that decides
it:

* a verb whose class is `compensatable` MUST fill its `compensate` slot
  (`` `ui.text[ui.text]` is compensatable, so extern `type_amount` must
  declare `compensate` ``);
* a verb whose class is `NO_INVERSE` (`irreversible`, `unknown`) MUST NOT
  (`` `ui.download[ui.download]` is irreversible, so extern `fetch` may not
  declare `compensate` ``).

Both are raised with `code="G4"`, `category="reversibility"`. The obligation
is real, and the code the checker credits it to is its sibling G4's — which is
the fact this row exists to make checkable and the fact the STATUS row now
records, instead of sitting at `none`.

## The model

`Legal` is the checker's rule over one extern declaration: the declaration's
capability token, its registry class, and whether its `compensate` slot is
filled. `legalB_iff` bridges the oracle's `A5` row. `Registers` is A5's own
half — "an emission must register a compensation" — and `ClaimsNothing` is
the other half of the same rule, whose argument belongs to the residue report
rather than to this guarantee (a declared inverse on a class with no inverse
would let `no_residue` be printed for a step that left residue). Both halves
are stated because `teardown_refusal` decides them in one function: a row
that modelled half of it would report the other half's refusals as
agreements.

## What is restated, and why

`verbClass` and the token splitter are written out here rather than imported,
the same way `RevL.G4Inverse` restates its tables: a widening of the
checker's registry then moves the checker alone, and the disagreement
surfaces as the harness's fatal `missed-A5` instead of being absorbed.

## What this does not cover

The rule's DOMAIN is the declared capability token of an extern. It says
nothing about a service method's `emission[...]` scope (a declaration of an
interface, not a crossing, as `teardown_refusal`'s own docstring says),
nothing about the ladder's admissibility (the G8 refusals in `ui_family.py`:
a root alone, an undeclared verb, a rung the slice has not admitted), and
nothing about whether a declared inverse actually inverts anything — that is
the `INV` row's question (issue #2097). The refusal this row explains is
raised at PARSE, so a corpus document carrying it produces no facts at all:
the row decides the admitted corpus, and the `a5_coverage` ratchet reads the
refusal off the parse census's own reported code.
-/

namespace RevL.A5

/-- The reversibility classes `ui_family` names. -/
inductive Cls where
  | reversible
  | compensatable
  | confirmRequired
  | irreversible
  | unknown
  deriving DecidableEq, BEq, Repr

/-- `ui_family._bare`: the token with any item-294 parameter valuation
stripped. A narrowed `ui.click(host="a")` is the same operation as
`ui.click`; a valuation that could change the class would be an author-side
opt-out. Written over `List Char` because `String.splitOn` is opaque and does
not reduce in the kernel, so no theorem could be decided on it. -/
def bareOf (token : String) : String :=
  String.ofList (token.toList.takeWhile (fun c => c ≠ '('))

/-- `"."`-splitting, structurally, for the same reason. -/
def splitDots : List Char → List String
  | [] => [""]
  | c :: cs =>
    if c = '.' then "" :: splitDots cs
    else
      match splitDots cs with
      | [] => [String.ofList [c]]
      | h :: t => String.ofList (c :: h.toList) :: t

/-- `ui_family.REVERSIBILITY`, keyed by the `(root, verb)` segments a token
splits into: the classes the registry owns, restated in-module. -/
def verbClass : List String → Option Cls
  | ["screen", "observe"] => some .reversible
  | ["ui", "find"] => some .reversible
  | ["ui", "text"] => some .compensatable
  | ["ui", "click"] => some .unknown
  | ["ui", "download"] => some .irreversible
  | _ => none

/-- `ui_family.reversibility`, verbatim: the exact registered token first,
then the VERB a rung or a valuation descends from. A rung is NOT an escape
hatch — `ui.text.selector` resolves to `ui.text`'s class, so a rung cannot
arrive carrying a weaker obligation than the verb it descends from just
because it is spelled differently. `none` outside the family. -/
def classOf (token : String) : Option Cls :=
  match splitDots (bareOf token).toList with
  | root :: verb :: _ => verbClass [root, verb]
  | _ => none

/-- The reversibility classes for which a declared inverse is a FALSE
CLEANLINESS CLAIM, `ui_family.NO_INVERSE`. -/
def noInverse : List Cls := [.irreversible, .unknown]

/-- One extern declaration's computer-use capability, as the exporter reads
it: the token it declares and whether its `compensate` slot is filled. -/
structure Decl where
  name : String
  token : String
  compensate : Bool
  deriving DecidableEq, Repr

/-- **The A5 obligation.** A declaration whose token's reversibility class is
`compensatable` must fill its `compensate` slot. -/
def Registers (d : Decl) : Prop :=
  classOf d.token = some .compensatable → d.compensate = true

/-- The other half of the same rule: a class with NO INVERSE may not declare
one. Stated over the class the registry resolves the token to, because a
token with no class at all satisfies both halves vacuously. -/
def ClaimsNothing (d : Decl) : Prop :=
  ∀ c, classOf d.token = some c → c ∈ noInverse → d.compensate = false

/-- `ui_family.teardown_refusal`, restated: both halves hold. A declaration
outside the family satisfies both vacuously, which is the rule. -/
def Legal (d : Decl) : Prop := Registers d ∧ ClaimsNothing d

/-- The A5 half, as the decider. -/
def registersB (d : Decl) : Bool :=
  match classOf d.token with
  | some .compensatable => d.compensate
  | _ => true

/-- The no-inverse half, as the decider. -/
def claimsNothingB (d : Decl) : Bool :=
  match classOf d.token with
  | some .irreversible => !d.compensate
  | some .unknown => !d.compensate
  | _ => true

/-- The rule, as the decider the oracle runs. -/
def legalB (d : Decl) : Bool := registersB d && claimsNothingB d

/-- The rule over a whole file's declarations. -/
def A5OK (decls : List Decl) : Prop := ∀ d ∈ decls, Legal d

/-- The rule over a whole file's declarations, as the decider. -/
def a5B (decls : List Decl) : Bool := decls.all legalB

theorem registersB_iff (d : Decl) : registersB d = true ↔ Registers d := by
  unfold Registers
  cases hc : classOf d.token with
  | none => simp [registersB, hc]
  | some c => cases c <;> simp [registersB, hc]

theorem claimsNothingB_iff (d : Decl) :
    claimsNothingB d = true ↔ ClaimsNothing d := by
  unfold ClaimsNothing noInverse
  cases hc : classOf d.token with
  | none => simp [claimsNothingB, hc]
  | some c => cases c <;> simp [claimsNothingB, hc]

theorem legalB_iff (d : Decl) : legalB d = true ↔ Legal d := by
  unfold legalB Legal
  rw [Bool.and_eq_true, registersB_iff, claimsNothingB_iff]

theorem a5B_iff (decls : List Decl) : a5B decls = true ↔ A5OK decls := by
  unfold a5B A5OK
  rw [List.all_eq_true]
  refine forall_congr' fun d => ?_
  exact imp_congr_right fun _ => legalB_iff d

/-! ### The string interface the oracle's row runs on

The `A5` row carries the DECLARED TOKEN, not a class the exporter computed, so
the class is decided here from the restated registry — the `SW` row's
`legalCols` arrangement. A row whose column does not parse decides NOTHING
(`false`) rather than guessing, the same fail-closed reading
`G4Witnessed.clsOfString` gives an unnameable classification. -/

/-- The rule at one row's columns. -/
def legalToken (token : String) (compensate : Bool) : Bool :=
  legalB ⟨"", token, compensate⟩

/-- The `compensate` column: `yes` is filled, `no` is empty. -/
def parseCompensate (s : String) : Option Bool :=
  match s with
  | "yes" => some true
  | "no" => some false
  | _ => none

theorem legalToken_iff (token : String) (compensate : Bool) :
    legalToken token compensate = true ↔
      Legal ⟨"", token, compensate⟩ :=
  legalB_iff _

/-- The rule at a ROW's two string columns: the declared token and the
`compensate` column as the row prints it. An unreadable column decides
nothing (`false`), never a vacuous `ok`. -/
def legalCols (token compensate : String) : Bool :=
  match parseCompensate compensate with
  | some b => legalToken token b
  | none => false

/-- `legalCols` is exactly the rule at the carried columns, and is `false` —
never vacuously `true` — when the `compensate` column does not parse. -/
theorem legalCols_iff (token compensate : String) :
    legalCols token compensate = true ↔
      ∃ b, parseCompensate compensate = some b ∧ Legal ⟨"", token, b⟩ := by
  unfold legalCols
  cases hc : parseCompensate compensate with
  | none => simp
  | some b => simp [legalToken_iff]

/-- A verdict of `false` is the rule VIOLATED. -/
theorem not_legal_of_legalB_false (d : Decl) (h : legalB d = false) :
    ¬ Legal d := fun hL => by
  have h1 : legalB d = true := (legalB_iff d).mpr hL
  rw [h1] at h
  exact Bool.noConfusion h

/-! ### The two directions -/

/-- A `compensatable` declaration with an empty `compensate` slot is refused:
the emission's inverse exists and only the author can write it. -/
theorem compensatable_without_compensate_refused (d : Decl)
    (h : classOf d.token = some .compensatable) (hc : d.compensate = false) :
    ¬ Legal d := fun hL => by
  have h1 : d.compensate = true := hL.1 h
  rw [hc] at h1
  exact Bool.noConfusion h1

/-- A class with NO INVERSE that declares a `compensate` is refused. -/
theorem no_inverse_may_not_declare (d : Decl) (c : Cls)
    (h : classOf d.token = some c) (hm : c ∈ noInverse)
    (hc : d.compensate = true) : ¬ Legal d := fun hL => by
  have h1 : d.compensate = false := hL.2 c h hm
  rw [hc] at h1
  exact Bool.noConfusion h1

/-- A `reversible` verb is required to declare nothing and refused for
nothing, so neither half of the rule reaches it. -/
theorem reversible_untouched (d : Decl)
    (h : classOf d.token = some .reversible) : Legal d :=
  ⟨fun hc => by rw [h] at hc; exact absurd hc (by decide),
   fun c hc hm => by
     rw [h] at hc
     exact absurd ((Option.some.inj hc).symm ▸ hm) (by simp [noInverse])⟩

/-- **A rung is not an escape hatch** (`ui_family.reversibility`'s own
doctrine): a token one rung down resolves to its VERB's class, so a rung
declared without a `compensate` is refused by the same rule as the verb. -/
theorem rung_is_not_an_escape_hatch :
    classOf "ui.text.selector" = some .compensatable
      ∧ classOf "ui.text.pixel" = some .compensatable
      ∧ classOf "ui.click.selector" = some .unknown := by decide

/-- An item-294 valuation does not move the class either, for the same
reason. -/
theorem valuation_does_not_move_the_class :
    classOf "ui.text(app=\"Billing\")" = some .compensatable
      ∧ classOf "ui.text.selector(app=\"Billing\")" = some .compensatable
      ∧ classOf "ui.download" = some .irreversible := by decide

/-- A token outside the namespace has no class, so the rule does not reach
it: this row claims nothing about a `db.write`. -/
theorem outside_the_family_is_out_of_the_row :
    classOf "db.write" = none ∧ classOf "ui" = none
      ∧ classOf "ui.drag" = none := by decide

/-! ### The corpus shapes -/

/-- `tests/fixtures/emit_py_corpus/ui_transaction_unit.rvl`: `type_amount`,
the corpus's one COMPENSATABLE crossing, and the one that declares its
inverse through `compensate clear_amount()`. -/
def typeAmount : Decl := ⟨"type_amount", "ui.text", true⟩

/-- The same declaration with the `compensate` clause removed — the A5
violation, and the shape
`examples/rejections/a5_compensatable_without_compensate.rvl` writes down. -/
def typeAmountUnregistered : Decl := ⟨"type_amount", "ui.text", false⟩

/-- `tests/fixtures/emit_py_corpus/ui_unit.rvl`: `actuate` on `ui.click`,
class `unknown`, no inverse declared — the admitted shape of a class with no
inverse. -/
def actuate : Decl := ⟨"actuate", "ui.click", false⟩

/-- `actuate` with a `compensate` clause added: the no-inverse half's
violation, and the shape
`examples/rejections/a5_no_inverse_declares_compensate.rvl` writes down. -/
def actuateClaiming : Decl := ⟨"actuate", "ui.click", true⟩

/-- `tests/fixtures/emit_py_corpus/ui_transaction_unit.rvl`: `read_pane` on
`screen.observe`, class `reversible`, which neither half reaches. -/
def readPane : Decl := ⟨"read_pane", "screen.observe", false⟩

theorem fixtures_decided :
    legalB typeAmount = true ∧ legalB typeAmountUnregistered = false
      ∧ legalB actuate = true ∧ legalB actuateClaiming = false
      ∧ legalB readPane = true := by decide

/-- **Non-vacuity.** The rule refuses the `compensatable` declaration with no
`compensate` and admits the same declaration with one; it refuses the
`unknown` declaration that declares one and admits the same declaration
without; and it does not reach the `reversible` one. So the printed `A5`
verdict is mutation-sensitive in both directions, and neither half of the
rule is vacuous. -/
theorem a5_not_vacuous :
    ¬ Legal typeAmountUnregistered ∧ Legal typeAmount
      ∧ ¬ Legal actuateClaiming ∧ Legal actuate ∧ Legal readPane :=
  ⟨not_legal_of_legalB_false _ (by decide),
   (legalB_iff typeAmount).mp (by decide),
   not_legal_of_legalB_false _ (by decide),
   (legalB_iff actuate).mp (by decide),
   (legalB_iff readPane).mp (by decide)⟩

/-- **The witness bites.** The two `ui.text` declarations differ in nothing
but the `compensate` column, and the verdict flips; the two `ui.click`
declarations likewise. Emptying or filling the column is therefore what moves
the row, which is what the `a5_coverage` ratchet re-decides on the corpus.
The `reversible` pair does NOT flip, which is the rule not reaching it. -/
theorem witness_bites :
    legalB ⟨"type_amount", "ui.text", true⟩ ≠
        legalB ⟨"type_amount", "ui.text", false⟩
      ∧ legalB ⟨"actuate", "ui.click", false⟩ ≠
        legalB ⟨"actuate", "ui.click", true⟩
      ∧ legalB ⟨"read_pane", "screen.observe", false⟩ =
        legalB ⟨"read_pane", "screen.observe", true⟩ := by decide

end RevL.A5
