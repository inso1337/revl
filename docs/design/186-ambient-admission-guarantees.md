# 186: ambient admission, the guarantee story, and what is deferred

**Roadmap:** item 186 (Path C, folds 419c/419f) · **Issue:** #86 · **Landed:** slices 1-2 in docs/design/186-ambient-admission.md (`admit_ambient(src, manifest)`: G2 and multi-realm routes against a provisions-only wire) · **Status:** DECISION, 2026-09-08. Lower priority; one bounded slice now, the replacement wave deferred behind named preconditions.

## The decision

**Yes, a component may be admitted against the ambient running manifest, and
the self-host gate must be able to say so.** This is not new behaviour for
revl: the reference already does it on every `revl_load`, `swap` and per-turn
`admit` (`compile_files(paths, manifest=running, replacing=[...])`,
`src/revl/compiler.py`: ambient services in scope, G2/G3 spanning both, a
same-named component implicitly replacing the running one). What is missing is
parity in `selfhost/lower.rvl`, and parity matters for one reason: the native
gate (`crates/revl-gate`, the wasm edge gate, the polyglot mesh of 337) is what
a receiving tier re-admits against, and a gate that can only answer "does this
one text admit" cannot serve hot-swap at a seam. Ambient admission is what makes
337's "re-admit at the receiver against its local running manifest" a sentence
about the native gate rather than the py one.

The shape stays `admit_ambient(src: Str, manifest: Str) -> Str`. The wire grows
by tagged row kinds rather than by a third argument, so the crate signature the
gate digests does not move.

What is deferred, with preconditions: replacement (hot-swap, `replacing`), the
`handoff` state contract, and the differential oracle that constructs a running
manifest on both sides. Each is designed below so the deferred wave builds
against a decision, not a blank.

## The guarantee story under ambient admission

Let `M` be the running manifest, `X` the incoming text, `R ⊆ M` the components
being replaced (empty today).

| guarantee | under ambient admission |
|---|---|
| G1 declared access | intrinsic to `X`. The manifest contributes only which keys exist and which service each carries (`services` in the ambient), never a reach the text did not declare |
| G2 provision disjointness | over `(key, realm)` in `provisions(X) ∪ provisions(M \ R)`. Landed for `R = ∅`. A conflict is reported against the incoming component, byte-identical to the in-text wording |
| G3 acyclicity | over the union graph: `M`'s provider-to-consumer edges plus `X`'s. NOT landed: the wire carries no requirements, so a cycle that closes through `M` is not seen. Slice 3 below |
| G4, G5, G6, G8 | intrinsic to `X`; the manifest cannot add or remove an effect form, a mode, or an extern |
| G7 | unchanged for `X`; for `R`, the withdrawal runs the replaced component's own LIFO teardown, which is what makes replacement expensive and is why it is a wave |
| G9 taint | checked over `X`'s bodies with the running providers' declared return qualifiers taken from the ambient `services`; a running component's own sinks are not re-walked, because its text was admitted with its own flow and is unchanged |
| multi-realm routes (162) | a route may target a realm provided only by `M`. Landed (slice 2) |
| handoff (53) | the replacement's accepted state type must be compatible with the replaced component's exported type under the §5 relation. NOT landed in the self-host: needs the type layer |
| the halted state (443) | admission against a halted composition is refused; the reference's session already refuses `load`/`swap` after a halt, and the self-host gate refuses a manifest whose header row says `halted` (below) |

The base invariant that every slice keeps: `admit_ambient(src, "") ==
admit_src(src)` exactly, and a manifest holding only provision rows behaves
exactly as slices 1-2 do today.

## Replacement semantics (decided now, implemented in the wave)

- `replacing` is a set of running component names carried IN the manifest
  string (row kind `-C`), so `R` is a property of the admission call, never
  something the incoming text can assert about itself.
- G2 runs against `M \ R`. A component in `X` whose name matches one in `M` is
  implicitly in `R`, mirroring `compile_files`.
- **A replacement that would leave a live consumer unmet is refused by
  default.** If `R` provided `(k, r)` and some component in `M \ R` requires
  `k` in `r`, then `X` must provide `(k, r)`, otherwise the refusal names the
  consumer and the lost key. This is item 460's rule (a documented refusal with
  retained ownership beats an apparently successful replacement) and the
  row-level rule already in docs/composition-rows.md (a provision removed
  upstream is a refusal). There is no opt-in to strand consumers in this wave;
  a composition that wants a key gone unloads the consumer first.
- `handoff`: for each `(k, r)` in `R` with an exported state type `E`, the
  replacement's `handoff k: A` must satisfy `compatible(E, A)` in the value-flow
  direction docs/state-handoff.md fixes; a missing `handoff` on the replacement
  when `R` exported one is a refusal naming the dropped state.
- Atomicity is the runtime's: the gate only answers. The reference's `swap`
  boots, admits, re-points and then tears down (docs/swap.md); the native gate
  returns the same verdict string so a receiving tier can refuse before booting.

## The wire

Rows joined by `;`. Kind by the leading character:

```
C/k/r        provision: component C provides key k in realm r ("" = shared)   (landed)
C<k          requirement: component C requires key k                           (slice 3)
-C           replacing: component C is withdrawn by this admission             (wave)
C=k:T        handoff: C exports state of type T at key k                       (wave)
!halted      header: the composition is halted; every admission refuses       (slice 3)
```

Provision rows are exactly today's; a manifest of provision rows parses as
before. `parse_manifest` becomes `parse_manifest_rows` over the tagged kinds
and unknown kinds refuse by name rather than being skipped (a row that parses
and does nothing is worse than one that refuses).

Because `selfhost/lower.rvl` is a digest input of the gate crate, this slice
regenerates BOTH `tools/build_gate_crate.py` and `tools/build_gate_wasm.py`
outputs and runs `--check` on each.

## The differential oracle

Two oracles, one per regime:

- **Oracle A (no replacement, landed shape).** `admit_ambient(X, wire(M)) ==
  admit_src(M ++ X) == reference(M ++ X)`. Slice 3 extends it to G3: a cycle
  that closes through `M` refuses identically on all three legs.
- **Oracle B (replacement, the wave).** The equivalence above breaks, so both
  sides must derive the running manifest from ONE artifact. Compile `M` on the
  reference to `IR(M)`; a new projection `revl.manifest_wire(ir) -> str` in
  `src/revl/` renders `IR(M)` to the wire above; then
  `admit_ambient(X, manifest_wire(IR(M)) + ";-R...")` is compared to the first
  `TAG|message` of `compile_files([X], manifest=IR(M), replacing=R)`. The
  reference side needs a thin wrapper that returns that string; nothing else on
  the reference changes. This is the "construct a running manifest on both
  sides" the roadmap asked for, and it is byte-agreement on the verdict, in
  keeping with the self-host oracle discipline.

## Slices and preconditions

| slice | delivers | precondition |
|---|---|---|
| **3 (now)** | requirement rows on the wire; G3 over the union graph in `link_refusals(pg, seed)`; the `!halted` header; `manifest_wire(ir)` projection; oracle A extended to G3 and pinned in `tests/test_selfhost_lower.py`; gate crate and wasm gate regenerated | none beyond the regen |
| **wave, part 1** | `-C` rows, G2 against `M \ R`, the unmet-consumer refusal, oracle B | the reference wrapper returning the first verdict string |
| **wave, part 2** | `C=k:T` rows and the handoff compatibility check | the self-host type layer (docs/design/457-selfhost-type-layer.md): `compatible` cannot be ported to `lower.rvl` before the types it compares exist there |
| **wave, part 3** | 419c closure: line-ordered collecting of ambient refusals against internal ones for the multi-refusal corpus (slice 2 ordered the single-conflict case) | parts 1-2, so the corpus can carry multi-refusal programs |

Slice 3 is small, needs no type layer, and closes the one guarantee hole that
is a soundness gap today (a cycle hidden by the manifest). The wave is deferred
until the type layer lands, and this note is the spec it lands against.

## Relation to the other decisions in this batch

- The `host` rows of docs/design/457-endpoint-one-definition.md are ambient
  provisions from the runtime's point of view: `Host/server/` and `Host/auth/`
  are provision rows in the wire, and a component `requires auth: Auth` admits
  against them through exactly this entry point. No new mechanism.
- A halted composition (docs/design/443-estop-tier-contract.md) is a manifest
  with the `!halted` header, so the native gate refuses admission against it
  for the same reason the py session does.

## Exit tests

1. **G3 through the manifest.** Running `A provides a requires b`; admit `B
   provides b requires a`. Refused, naming the cycle, byte-identical on
   `admit_ambient(B, "A/a/;A<b")`, `admit_src(A ++ B)` and the reference.
   Without the requirement row (today's wire) the same admission wrongly
   admits; the test pins that the row is load-bearing.
2. **Base invariant.** `admit_ambient(src, "")` and a provisions-only manifest
   are byte-identical to before slice 3 across the existing corpus.
3. **Halted header.** `admit_ambient(X, "!halted;A/a/")` refuses every `X`,
   naming the halt.
4. **Unknown row kind.** `admit_ambient(X, "?A/a/")` refuses naming the row.
5. **Oracle B (the wave's exit).** For a corpus of `(M, X, R)` triples covering
   plain replacement, an unmet consumer, a realm-separated non-conflict and a
   handoff mismatch, the first verdict string agrees byte-for-byte between the
   reference wrapper and `admit_ambient` over `manifest_wire(IR(M))`.
