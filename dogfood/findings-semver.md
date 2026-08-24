# Findings — item 64, derived semantic versioning (agent/wave64-semver)

The feature was delivered by an interrupted predecessor agent: the uncommitted
worktree carried `src/revl/version.py`, `docs/derived-versioning.md`,
`tests/test_derived_version.py` and the `revl version` CLI wiring, all green
(15/15 tests). This agent's job was assessment, completion, and hardening: it
committed the base work, then added seven targeted tests for the roadmap's
remaining named cases (widened param/return, narrowed return, capability-scope
narrow/widen, service-wide commutative flip, arity change) and polished the
patch-case wording. The implementation itself was already correct — it hands a
single-method projection of every shared operation to the real
`admission._service_compatible` predicate and names the emission-gain (`kind`
`emission`, major) and emission-loss (`kind` `emission-loss`, minor) cases.

## 1. Refusal log

No `revl compile` rejection of *feature* source: the implementation was
already green and needed no refusal→fix cycle. Two probe rejections, both
self-inflicted harness errors, both cleanly caught:

- `expected :, found ')'` (parser) — I tried an *untyped* parameter in a
  service declaration (`fn get(key)`). Service declarations require typed
  parameters; provide bodies are where params may be untyped. Verdict:
  `caught-bug` — my probe was invalid and the checker was right; I rewrote the
  probe with the numeric widening `Int -> Float`.
- `method `get` of provision `s` takes 1 params but service Store declares 2`
  (lowering, A6 arity) — my arity-change probe kept a 1-param provider body
  against a 2-param redeclaration. Verdict: `caught-bug` — exactly the
  consistency the language promises; I fixed the provide block.

## 2. Friction log

- `[slow]` The drift semantics the whole design leans on ("§5") had to be read
  from `admission.py`/`lower.py` source: DESIGN.md §5 is a type-system sketch
  and does not name `_service_compatible` or its consumer/provider regimes.
  `docs/service-compat.md` does document the relation, but nothing points the
  roadmap reader at it from item 64.
- `[nit]` Ad-hoc probes of `revl.version` need `src` on `PYTHONPATH` (the test
  file inserts it manually; `pytest` bootstraps via conftest). First probe run
  failed with `ModuleNotFoundError` before I set it — standard for this repo,
  but the cost of a forgotten step.
- `[nit]` The patch-case CLI text said "no bump is required" while still
  printing `1.4.2 -> 1.4.3` — internally tense. Reworded to "the only
  permitted bump is a patch (bug fixes)".
- `[nit]` The one real expressiveness note: a *widened-to-untyped* parameter is
  not expressible in a service declaration, so the "widened parameter -> minor"
  test uses `Int -> Float` numeric widening instead. The predicate's
  `npt is None` branch (widened to untyped) is therefore only reachable via
  ambient/manifest inputs, not compiled source.

## 3. What revl gave you

- The drift predicate classified every case I probed correctly with **zero
  reimplementation**: emission gain → major, emission loss → minor, capability
  scope narrowing → minor / widening → major, widened param → minor, widened
  return → major, narrowed return → minor, arity and async flips → major,
  service-wide commutative flip → major. The version diff is a thin projection
  + join over the real admission verdict; the cross-check test pins `derive` to
  `_service_compatible` directly.
- The checker caught both of my probe mistakes (arity mismatch, untyped
  service param) with diagnostics that named the exact fix — the A6/G4-style
  consistency checks did real work even in throwaway probe scripts.

## 4. Time-to-green

No compile→refuse→fix cycles on feature code. Two red probe runs (my
construction errors, above), each fixed in one edit — call it 2 quick cycles.
Longest single stall: none meaningful; the longest single *reading* task was
confirming the consumer-regime drift verdicts from `admission.py` (~10 min).

## 5. Cost ledger

- `docs-gap` — reading `admission.py`/`lower.py` source to confirm the exact
  verdict set the design references; one cycle that a one-line pointer from
  DESIGN §5 (or from roadmap item 64) to `_service_compatible` /
  docs/service-compat.md would have removed.
- `tooling` — forgotten `PYTHONPATH` for ad-hoc `revl.version` probes; one
  lost run (~seconds). The repo's "tests bootstrap src" convention is fine;
  only my one-off scripts paid the tax.
- `diagnostic` — the two probe rejections each cost one edit; both diagnostics
  were exact ("takes 1 params but service Store declares 2", "expected :,
  found ')'"), so nothing here was wasted — logged for completeness.

Single change that would have cut the most cost: a DESIGN §5 sentence naming
`admission._service_compatible` as the operative relation (with its
consumer/provider regimes), so versioning, plan, and the admission gate all
point at one documented predicate instead of three readers re-deriving it
from source.
