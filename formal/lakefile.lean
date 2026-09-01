import Lake
open Lake DSL

/-!
revl's formal backbone — plain Lean 4 core, no Mathlib.

No Mathlib is a deliberate choice: builds stay in seconds, which is what
keeps agentic proof iteration cheap, and nothing in the guarantee theorems
needs more than core's `List`/`Nat`/inductive machinery.

The dependency layering (formal/STATUS.md) is enforced by import structure:

  L0  RevL.Syntax / RevL.Typing / RevL.Semantics   architect-owned, frozen
  L1  RevL.Lemmas.*                                worker farms, pure utilities
  L2  RevL.Theorems.G*                             one file per guarantee;
                                                   workers may import L0/L1,
                                                   never each other.
-/

package «revl-formal»

@[default_target]
lean_lib «RevL» where
  roots := #[`RevL]
