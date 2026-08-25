# findings — selfhost/lower.rvl, Path C final slice (item 184)

Porting the last tractable admission surface into `selfhost/lower.rvl`: async
coloring rule 2, the code-less spawn-form refusals (bind-to-a-handle, unknown
target), `handoff` admission, and `isolate` target validation. Reference is
`src/revl/lower.py`; the oracle is `tests/test_selfhost_lower.py`.

## 1. Refusal log
No `revl compile` rejection of my own code this run — the gate compiled clean on
the first emit after each edit. The only "refusal" I hit was the differential
oracle catching an eyeballed in-file `test` literal that had drifted from the
reference (I wrote `` `kv` is isolated twice `` where the reference says
`` key `kv` is isolated twice ``). Verdict: **caught-bug** — the in-file-test ->
oracle audit did exactly its job, flagging a hand-typed literal before it could
be mistaken for ground truth.

## 2. Friction log
- `[nit]` No default parameter values / partial application: threading one new
  accumulator (`hoff: Bool`) through `p_comp_body` meant editing all 6 recursive
  call sites plus the entry call by hand. Mechanical, but every one is a place to
  drop a positional arg. `missing-feature`.
- `[nit]` Adding a field to a widely-constructed record (`FnD` gained
  `asyncParams`, `Stmt` gained a new `kind`) has no "construct-site" assist — I
  had to grep for every literal, and `p_fn` builds `FnD` as a bare `{...}` literal
  rather than through `mk_fnd`, so the two construction paths drift apart. The
  checker's exhaustive missing-field error is what kept this safe (see section 3),
  but finding the sites is manual. `tooling`.
- `[nit]` Mirroring the reference's `stop_async_arrows` (callee collection that
  stops at an async-flagged arrow) is not cheap in the gate: callee NAMES are
  collected at parse time, before the global async-slot table that would tell me
  which arrows are coerced exists. So the gate over-collects callees inside
  coerced arrows. Rule 2 masks the divergence for every arrow whose callee
  actually calls its async-typed slot (the callee is then rule-2-colored and
  colors the caller by the same path the reference uses), leaving only an
  async-typed-but-uncalled-parameter residue that no fixture reaches.
  `spec-ambiguity` — the reference carries the arrow color on the lowered IR; a
  verdict-only gate has no equally cheap place to recompute it.

## 3. What revl gave you
- The checker's **exhaustive record-field requirement** turned a risky refactor
  (two new fields on hot records) into a mechanical one: every missing-field
  construction site was a hard compile error, so there was no silent
  `asyncParams: []` default hiding an un-updated path. This is the single reason
  adding `asyncParams`/the new `Stmt` kind took one pass, not a debugging hunt.
- The **differential oracle** is the whole safety net: 12 new checks, each one
  verified byte-identical to `lower.py` on both an accepted and a rejected twin,
  and the in-file-test audit re-routes the .rvl's own `test` literals through the
  same oracle — which is how the drifted `isolated twice` literal surfaced
  instantly.
- **Monotone fixed points compose cleanly**: rule 2 was a one-line change to
  `async_colored` (`|| any_in(f.callees, f.asyncParams)`) because the coloring is
  already a least-fixed-point over a finite name set — the new seed just widens it.

## 4. Time-to-green
~3 compile->emit->cross-check cycles for the whole slice (rule 2; then spawn-form
+ handoff + isolate together; then the corpus/in-file tests). Longest stall was
zero real debugging — the one red was the self-inflicted test literal, fixed in
one edit. The empirical reference-probe step (running `compile_source` on each
candidate program to capture the exact code-less message BEFORE writing the gate
string) is what made time-to-green short: I never guessed a diagnostic's wording.

## 5. Cost ledger
- `tooling` — locating every construction site of a record after adding a field
  (grep, not an IDE affordance). Minutes, not a blocker.
- `spec-ambiguity` — the `stop_async_arrows` arrow-color model lives on the
  lowered IR in the reference; deciding how much of it a verdict-only gate must
  reproduce (answer: rule 2 masks the observable part) took the most *thinking*
  time this run, though no failed cycles.
- Single highest-leverage change: a way to **carry arrow coercion state without
  re-deriving it** (i.e. the gate lowering arrows to a node that records `async`,
  as the reference IR does) would let the gate mirror `stop_async_arrows` exactly
  and retire the last coloring approximation.
