# 414 - Reach-completeness harness (security-campaign capstone)

## Why

The 2026-08-31 adversarial review passes found ~14 CRITICAL-class holes in the
shipped security features. Every one was the same shape: an authority-derivation
surface (a fold, a gate, an audit) that visits ONE crossing kind and silently
misses another. The capability is reached but not seen, so the guarantee built on
that surface is a false-safe.

Concrete instances found:
- 245/246-F1: the approval class fold tested MEMBERSHIP of a grant's cap in the
  worst-class reach set, not SUBSET of the class-(c) reach under the grants.
- revocation-F1: revoke matched the minted key, not the coverage predicate.
- cap-algebra CRITICAL: the class fold followed the `req` service seam but not the
  spawn/instance-get seam, so a spawned class-(c) emission skipped the 246 prompt.
- 330 / 329-transitive: the untrusted-author reach sweep walked the root's direct
  imports, not the transitive module closure `compile_files` actually merges.
- 363-F1 / seam-C: the distribution-seam resource refusal matched a type NAME
  string, not a structural walk, so a resource nested in a record/variant/generic
  crossed.
- seam-A: the resource refusal ran only on cross-tier seams, not same-tier
  cross-process seams.
- 410-escape: `resolve_use` stamped install-origin from the matching search-path
  base without a realpath-containment check, so a `..` path forged stdlib-origin.

These are not independent bugs. They are one missing invariant: **every
authority-derivation surface must visit every crossing kind.** Re-discovering the
next instance by hand is unbounded work. This item makes the invariant a test.

## The two axes

### Crossing kinds (the columns) - every way a component reaches authority

1. `req` service-seam emission - `emit svc.method()` on a required service key.
2. spawn / instance-get emission - `emit s.inner.method()` on a spawned handle.
3. direct host-body extern - a bare-name call to an imported extern with a body.
4. TRANSITIVE host-body extern - reached through an imported `pub fn` wrapper in a
   non-root module (the whole merged closure, not just the root's direct imports).
5. same-tier cross-process seam - a value crossing between two same-backend
   processes.
6. cross-tier seam - a value crossing between two different-backend processes.
7. resource nested in an aggregate - a resource handle inside a record field, a
   variant case payload, a generic instantiation, an Opt/List/tuple.
8. first-class emitting callable / `*` widening - an emitting fn escaping in value
   position.
9. deferred class-(b) emission - a `deferred` emission enqueued for commit.
10. witnessed class-(a) crossing - a `witnessed` op with a registered inverse.

### Authority-derivation surfaces (the rows) - every fold that must be complete

A. approval ClassMap fold (`mcp/approval.py`) - the class-(a)/(b)/(c) decision that
   gates the 246 prompt.
B. `policy.component_reach` (`policy.py`) - the item-33 policy reach.
C. G8 audit boundary (`__main__._boundary` / `query.py`) - what `revl audit` prints.
D. untrusted-author reach sweep (`admit_profile.py`) - the 329/330 host-reach refusal.
E. distribution-seam resource refusal (`distribute.py` / `placement.py`) - the
   value-copy authority strip.
F. taint origin fold (`taint.py`) - the 249 provenance flow.
G. capability subset / attenuation (`admission.py` / `lower.py`) - the emission[caps]
   algebra.

## The matrix

For each (surface, crossing-kind) cell where the crossing is IN SCOPE for that
surface, the test constructs a minimal composition exercising that crossing and
asserts the surface SEES it (the fold attributes its class/caps/origin/refusal to
the reaching component). A cell that is legitimately out of scope for a surface is
marked N/A with a one-line reason, so the matrix is exhaustive by construction and
a reviewer can see there are no silent blanks.

The high-value cells (the ones a hole hid in) MUST be non-N/A and asserted:
- A x {1,2,8}  (the spawn-seam bypass was A x 2)
- D x {3,4}    (the transitive-closure hole was D x 4)
- E x {5,6,7}  (same-tier was E x 5; nested-resource was E x 7)
- A/B x 2      (the fold must follow the spawn seam)
- G x {attenuation widen, empty-set, `*`}

## Shape of the test

`tests/test_reach_completeness.py`:
- A table `CROSSINGS` of (id, builder) where builder returns a minimal source/IR
  exercising exactly that crossing kind.
- A table `SURFACES` of (id, prober) where prober runs the surface over a
  composition and returns the set of components/caps/classes it attributed.
- A parametrized test over the in-scope cells asserting the crossing's reaching
  component appears in the surface's attribution (or the documented refusal fires).
- A guard test that fails if a new crossing kind is added to `CROSSINGS` without a
  scope decision for every surface - so the enumeration cannot silently rot. When
  revl gains a new crossing kind, this test forces a row/column review.

## Dependencies

Build AFTER the in-flight spawn-fold (A x 2), seam (E x {5,7}), and grant/revoke
(A x 1 subset) fixes land, since those cells only pass once the holes are closed.
The N/A scope decisions and the crossing/surface tables can be written first.

## Non-goals

Not a fuzzer and not a proof. It is a completeness CHECKLIST the type system
cannot forget: it enumerates the known crossing kinds and pins every fold to all
of them. A crossing kind nobody has thought of is still out of its reach - the
mitigation for that is the review passes, not this test. What this converts is the
KNOWN surface from "re-audited by hand each pass" to "asserted every CI run".
