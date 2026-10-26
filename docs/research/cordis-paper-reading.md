# Reading note: Cordis and spatiotemporal composability

**Paper:** [A Programming Paradigm for Spatiotemporal Composability, arXiv
2608.25512v1](https://arxiv.org/abs/2608.25512v1), Yifan Shi, Wei Zhang, and
Tianyi Cui. The arXiv record was submitted on 2026-08-26. The record names no
publication venue, so this note cites it as a 2026 arXiv preprint. The current
paper link was checked on 2026-10-21.

The paper formalizes *temporal composability* (revertible effects with
accumulated inverses) and *spatial composability* (declared dependencies and
reactive lifecycle changes), then gives a calculus for their interaction. The
claims below summarize the results in §4.3 and their stated assumptions. They
are results about the calculus; they do not establish that an arbitrary
TypeScript implementation satisfies the calculus.

## Results and assumptions

| Result | Statement in the paper | Main hypotheses and limits |
|---|---|---|
| Theorem 64, Preservation | A well-formed registry remains well formed after any calculus step. | The current registry is well formed and the step is one of the defined rules. This is preservation of the formal registry invariants, not a proof about all host-language behavior. |
| Theorem 68, Recovery exactness | Applying a component's accumulator leaves the tables equivalent to applying the same other-fiber maps to the episode-start state. | The episode is open and the compared point lies within it. Effect iterators supply inverse witnesses; coeffect declarations supply commutativity witnesses; these restrictions support the pairwise-independence argument. Reading this as "as if the component had never begun" additionally requires that no fiber it instantiated takes a step during the interval. The result is only up to per-key equivalence, not physical restoration of every external state. |
| Corollary 69, Terminal recovery | When an episode closes, its component table is empty and the other tables match the state obtained by applying the intervening steps without the episode. | The episode opens and closes under the calculus rules; the corollary follows from Theorem 68. It is not a claim that an external system has no residue beyond what the calculus represents. |
| Theorem 70, Ordering | A component can begin only when its dependencies are provided; a provider stays available while an active consumer's episode relies on it. | A lifecycle step follows the calculus rules, and dependency views are committed as specified. The single-provider registry invariant and the unload guard are essential to the ordering result. |
| Theorem 71, Resolution coherence | The iterations in one transition use the dependency resolution committed when that episode began; a changed target diverts the transition rather than silently mixing resolutions. | The episode begins against a fixed resolution and follows the calculus's iteration and diversion rules. |
| Theorem 73, Progress | With an acyclic precedence relation, each component's iterator bounded by `K`, a finite set of names, and lifecycle-rule steps, every non-quiet state admits a step and every maximal lifecycle sequence reaches quiescence. The paper gives the per-name bound `S(n) ≤ (K + 3)(V(n) + 1)`. | The acyclicity, iterator bound, name-set finiteness, and rule restriction are explicit assumptions; this is not an unconditional termination result for arbitrary code. |
| Theorem 80, Confluence | Given a quiescent endpoint, the paper identifies a canonical ordering of lifecycle episodes; any two such sequences with the same orchestration steps reach states related up to renaming and the paper's state equivalence. | The precedence relation is acyclic; the sequence reaches quiescence; components are total on their provisions; the active set and other objects are as defined in §4.3.5. The proof also uses the paper's commutation and finiteness results. |

Section 6.1 treats the inverse's correctness as an obligation supplied by the
component author: the calculus/runtime carries the inverse, but does not verify
that it truly undoes the host operation. Section 6.3 describes dependency
declarations and context interception as capability-style access control over
proxy-mediated properties. The same section says that sandboxing untrusted
code from the host requires an external sandbox. Section 6.4 reports a
TypeScript Cordis implementation while describing the paradigm as
language-agnostic. The reported implementation is TypeScript; the paradigm
and other implementations are not limited to that language.

## Effect on P01 and P23

- **P01, confirmed with a narrower explanation:** Cordis is the source of the
  revertible-effects discipline and the paper supplies formal composition
  results. The inverse and commutativity conditions are witnesses the model
  assumes. P01's description of revl is narrowed to checking declaration shape
  and capability reach; it does not claim revl proves an inverse correct.
- **P23, narrowed:** the paper does not prove all of the guarantees from the
  existing draft as unconditional implementation properties. The theorem names
  and their assumptions are listed above. revl checks some corresponding
  structural conditions, but does not discharge semantic host-code obligations
  or prove that a Cordis implementation satisfies the calculus.
- **Capability coverage, narrowed:** Cordis's dependency mediation and
  interception are a form of access control on proxy-mediated dependencies,
  not a complete sandbox for untrusted code. revl's static capability analysis
  is a separate check on the declared and transitively reachable source-level
  boundaries.
- **Runtime wording, clarified:** the paper reports a TypeScript implementation
  of a language-agnostic paradigm. P23 now distinguishes that implementation
  from revl's own backend targets without claiming that every Cordis implementation uses TypeScript.
- **Prior-art wording, removed:** neither draft uses this reading to make a
  novelty claim or to say revl invented reversible effects. The bounded source
  comparisons are in the [related-work table](related-work.md).
