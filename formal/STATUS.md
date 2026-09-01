# revl formal backbone — proof status

Rules of the layering (enforced by imports, not by hope):

- **L0** (`RevL.Syntax`, `RevL.Typing`, `RevL.Semantics`) is
  architect-owned and frozen. Worker sessions do not edit it; a needed L0
  change blocks on the architect.
- **L1** (`RevL.Lemmas.*`) is the lemma farm. Farm files import L0 only
  (ideally core only) and never each other.
- **L2** (`RevL.Theorems.G*`) is one file per guarantee, one session per
  file. Worker files import L0/L1, **never each other**. Theorem names go
  into `CheckAxioms.lean` the moment they are stated.
- A theorem counts as *proved* only if the axioms gate passes for it:
  no `sorryAx`, no project-defined axiom. Lean's three standard
  foundation axioms (`propext`, `Classical.choice`, `Quot.sound`) are
  whitelisted; anything else fails `make formal`. (`#print axioms` on a
  `sorry`'d declaration reports `sorryAx`, so an unfinished proof can
  never pass.)

## Theorem status

| Theorem | Guarantee (DESIGN.md §4) | Status | Axioms | Notes |
|---|---|---|---|---|
| `RevL.G1.declared_only_access` | G1 — declared-only access (Def. 25) | **proved** | none | component level; composes `TypedIn` over a body |
| `RevL.G4.inverse_or_emit` | G4 — inverse-or-emit (Def. 8) | **proved** | none | content is the shape of `Typed` |
| `RevL.G5.teardown_registers_nothing` | G5 — teardown registers nothing | **proved** | none | undo bodies are a separate constructor set |
| `RevL.G6.confinement` | G6 — confinement (Def. 48) | **proved** | none | content is the shape of `TypedIn`/`ReachIn` |
| `RevL.G7.teardown_replays_all` | G7 — LIFO-completeness (Thm. 16) | **proved** | none | every witnessed inverse is replayed |
| `RevL.G7.teardown_only_witnessed` | G7 | **proved** | none | nothing unwitnessed is replayed |
| `RevL.G7.teardown_eq_reversed_inverses` | G7 | **proved** | none | the LIFO equation; positions via `List.getElem_reverse` |
| `RevL.Semantics.teardown_length` | G7 — length form | **proved** | `propext` | one replay per witnessed effect |

## TODO (in dependency order)

1. **G2/G3**: graph properties over the linker manifest (provision
   disjointness, acyclicity) — small, but wait for the manifest model in
   L0 (a `Component`-graph + topological link judgment).
2. **G8**: boundary surface enumerability — needs the extern/emission
   declaration model in L0.
3. **Capability ceilings/budgets** (2.0 features): parameterized
   capabilities over the `Ctx` model.
4. **Differential oracle** (`harness/diff_corpus.py`): a Lean
   decision procedure is the blocker; alternative is exporting verdicts
   from Lean to JSON and diffing in Python. Architect call. This is the
   highest-leverage parallel-safe item: it is what keeps many worker
   sessions mutating L0 honest (drift surfaces as a corpus mismatch).
5. **L3, deliberately deferred**: `Trusted[T]`/`Secret[T]`
   non-interference and WAL commit/abort discharge. Both extend L0 (taint
   is a checker feature, not part of the current core; commit/abort is a
   runtime state-machine refinement). Not near-term work.

## Conventions for worker sessions

- State the theorem first with `sorry`, register it in `CheckAxioms.lean`
  and in this file as *stuck/TODO*, then fill it. The gate stays honest
  because `sorryAx` fails the build — a red build on a stated theorem is
  the system working, not a regression.
- Porting map: DESIGN.md §4 row → paper object → `src/revl/lower.py`
  (checker side) → the `tests/` file that currently witnesses the
  guarantee by execution. The test is the *example suite* for the formal
  model, not a substitute for it.
