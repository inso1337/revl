/-!
# G-MODEL-PLACE: where a model call may run, and how far a model may reach

Issue #1811 (group 1). The checker refuses, with code G-MODEL-PLACE
(`src/revl/model_route.py`, `src/revl/lower.py`):

* **Placement** (category `model-placement`): a `route model` arm that sends
  a confidentiality origin (`confidential`) to a model role declared
  `off_device`, directly or through a council member that receives it;
* **Reach** (category `capability-attenuation`, item 519): a component that
  consults a model role whose declared `reaches [...]` is not covered by what
  the component holds. A model chooses which capability the component reaches
  for, so the component's effective ceiling is the pair's. A role with no
  `reaches` clause reaches the unnameable `*`.

## The model

`PlaceOK` is stated here over the placed (origin, residence) pairs of a
component's arms; `placeB_iff` bridges the oracle's `MPV` row. The reach rule
is the item-66 attenuation rule with a model-route edge in place of a spawn
edge, so the oracle's `MAV` row decides it with the same proved
`attenuatesB` (`RevL.CapCeilings.Attenuates`) the spawn-attenuation `W` row
uses, over the component's held set and the role's reach.

## What this does not cover

Which roles a component routes through or crosses (the edges), and whether
it consults a model at all, are the exporter's, read the way
`lower._model_reach_edges` and `_consults_a_model` read them. The council and
candidate-set shape rules, the action-name and duplicate-arm rules, and the
value-level origin ceiling (item 514) are not this row.
-/

namespace RevL.ModelPlace

/-- Where a model role runs. -/
inductive Residence where
  | onDevice
  | offDevice
  deriving Repr, DecidableEq

/-- One placed role of a route arm: the origin it receives and where it runs. -/
structure Placed where
  origin : String
  residence : Residence
  deriving Repr, DecidableEq

/-- **The rule.** A confidentiality origin is placed on the device only. -/
def PlaceOK (conf : List String) (arms : List Placed) : Prop :=
  ∀ a ∈ arms, a.origin ∈ conf → a.residence = .onDevice

/-- The rule, as the decider the oracle runs. -/
def placeB (conf : List String) (arms : List Placed) : Bool :=
  arms.all (fun a => !conf.contains a.origin || a.residence == .onDevice)

theorem placeB_iff (conf : List String) (arms : List Placed) :
    placeB conf arms = true ↔ PlaceOK conf arms := by
  unfold placeB PlaceOK
  rw [List.all_eq_true]
  refine forall_congr' fun a => imp_congr_right fun _ => ?_
  by_cases hc : a.origin ∈ conf <;> cases hr : a.residence <;> simp [hc, hr]

/-- A confidential origin placed off the device refuses the component. -/
theorem off_device_refused (conf : List String) (arms : List Placed) (o : String)
    (ho : o ∈ conf) (ha : ⟨o, .offDevice⟩ ∈ arms) : ¬ PlaceOK conf arms := by
  intro h
  exact Residence.noConfusion (h _ ha ho)

/-- An origin that is not confidential may go anywhere. -/
theorem open_origin_anywhere (conf : List String) (o : String) (r : Residence)
    (ho : o ∉ conf) : PlaceOK conf [⟨o, r⟩] := by
  intro a ha hc
  simp only [List.mem_singleton] at ha
  subst ha
  exact absurd hc ho

/-- Placing on the device is always admitted. -/
theorem on_device_admitted (conf : List String) (arms : List Placed)
    (h : ∀ a ∈ arms, a.residence = .onDevice) : PlaceOK conf arms :=
  fun a ha _ => h a ha

/-! ### The corpus shapes -/

def confOrigins : List String := ["confidential", "secret"]

/-- `gmodelplace_confidential_off_device.rvl`: `confidential -> cloud`, and
`cloud` is `off_device`. -/
def cloudArm : List Placed := [⟨"confidential", .offDevice⟩]

/-- `gmodelplace_council_member_off_device.rvl`: `confidential -> Release`,
whose members `vast` (off) and `edge` (on) both receive the origin. -/
def councilArm : List Placed := [⟨"confidential", .offDevice⟩, ⟨"confidential", .onDevice⟩]

theorem fixtures_decided :
    placeB confOrigins cloudArm = false ∧ placeB confOrigins councilArm = false
      ∧ placeB confOrigins [⟨"confidential", .onDevice⟩] = true
      ∧ placeB confOrigins [⟨"*", .offDevice⟩] = true :=
  ⟨rfl, rfl, rfl, rfl⟩

/-- **Non-vacuity**: the rule refuses the off-device placement and admits the
on-device one and an open origin off the device. -/
theorem placement_not_vacuous :
    ¬ PlaceOK confOrigins cloudArm ∧ PlaceOK confOrigins [⟨"confidential", .onDevice⟩]
      ∧ PlaceOK confOrigins [⟨"*", .offDevice⟩] :=
  ⟨fun h => absurd ((placeB_iff _ _).mpr h) (by decide),
   (placeB_iff _ _).mp rfl, (placeB_iff _ _).mp rfl⟩

end RevL.ModelPlace
