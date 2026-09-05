# Threat model — the gate itself

revl is an *admission gate*: the one promise it sells is that the checker
decides whether a component may enter a running system, and a component that
would corrupt that system cannot compile (docs/v2.0-roadmap.md, "Toward early
production §1"). The gate's input is, by construction, hostile: the components
it admits are agent-generated, and an agent optimising for "make the tool say
yes" will phrase a side effect however it must to get past a name-based check.

This document states what the gate defends, what it deliberately does **not**
defend (the non-goals, written down before someone discovers them the hard
way), and who the attacker is. It is the prose companion of the executable
attack suite in `tests/test_adversarial_gate.py` (and its seed,
`tests/test_mcp_hint_adversarial.py`), which turns every claim below into a
test that the gate refuses or surfaces the attack.

## Attacker model

The attacker is an **agent authoring a revl component** whose runtime behaviour
disagrees with what the component *advertises*. The agent controls the full
source: service declarations, component bodies, provide-method bodies, `extern`
host blocks, helper `fn`s, and how effects flow between them. The agent's goal
is authority it did not declare — an irreversible host effect from an operation
advertised `readOnlyHint: true`, a boundary crossing absent from the audit
surface, a second provider smuggled onto a key, a mutation with no inverse.

The attacker does **not** control the checker, the runtime, or the reviewer
reading `revl audit`. The threat is therefore *the gap between the declaration
and the body* — every attack below tries to make the body do more than the
declaration admits while still compiling.

What the attacker is **not** modelled as doing: attacking the host language
sandbox (that is a `@py`/`@ts` block's own trust boundary — see Non-goals), or
attacking the runtime's disposal ordering (a runtime property, verified by the
backend scenarios, not the gate). Those are real threats owned by other tiers;
naming them here keeps the gate's own promise honest.

## What the gate defends (G1–G9, the `Secret` families, T1–T3, A1–A3/A5/A6/A8/A9)

The guarantee families and where each is enforced are catalogued in
docs/rejections.md; the executable spec is `examples/rejections/` +
`tests/test_frontend.py`. This section reads them as *defences* — the property
an attacker would have to break — and the adversarial suite is the "can an
attacker get around the rejection?" layer over that spec.

| # | Defends against | Enforcement | Attacker's move, and why it fails |
|---|---|---|---|
| **G1** | a body reaching a service/field it never declared | lower, name-scoped | naming an undeclared key is refused; aliasing does not help — there is no name to bind from |
| **G2** | two providers racing for one key (per realm) | linker, provider table | a duplicate provider, incl. via an equal realm label, is a whole-composition verdict with a why-trace |
| **G3** | a dependency cycle that could never activate | linker | cycle is a composition-wide search; the diagnostic prints the loop |
| **G4** | an irreversible effect from an operation that advertises none | lower, emission fixed point | **the primary battleground** — see below |
| **G5** | teardown registering new effects | by construction (grammar) | an `effect`/`emit` in an `undo` is a parse error; there is no slot to attack |
| **G6** | unconfined work outside an effect form | parser/checker | a bare expression statement has no syntactic position |
| **G7** | teardown running in the wrong order | derived by lowering | order is compiler-derived; totality (`verified fn`) is the one checker lever |
| **G8** | a host reach that is not on the review surface | parser (extern classification) + audit | an `extern` must classify (`pure`/`acquire`/`emission`); the audit enumerates crossings — see the boundary section |
| **A1** | an iteration boundary where none can exist | lower | `await` in a provide method is refused; boundaries exist only during activation |
| **A2** | an acquisition after a provision (revertible under a live consumer) | linker | acquire-after-`provide` is a linker rule |
| **A6** | a provide-method that does not match its service signature | lower / compat gate | a method the service does not declare has no signature to check against |
| **A8** | a mid-body failure that tears or leaks | lower + runtime (L-Raise) | accumulated effects revert LIFO; the component lands FAILED |

### G4 is the primary battleground

G4 is where authority actually leaks, because it is the guarantee about
*effects*, and an effect can be phrased many ways. The load-bearing claim is
that `readOnlyHint: true` on a generated MCP tool means the compiler has
**proved** the operation reaches no irreversible host effect — a name-only walk
of call sites is not enough, because an emission can be handed around as a
value and fired one indirection later.

The gate's answer is an **emission fixed point** (`src/revl/emission_analysis.py`,
`src/revl/lower.py`) that treats a plain provide-method as declared read-only
and refuses it if it *reaches* any emission, where "reaches" is closed under:

- **direct calls** — `ship(a)` (the case that always worked);
- **transitive calls** — `method → a → b → c → ship`, incl. mutual recursion;
- **first-class references** — any bare use of an emitting callable's name
  outside call position: passed as an argument (`indirect(ship, a)`), bound to
  a local (`let g = ship`), returned from a helper (`getship()`), or stashed in
  a record/list literal (`{ f: ship }`, `[ship]`). Such a value can be
  dispatched by whoever receives it, so it reaches code no name-based bound can
  resolve — modelled as the capability `*`, the deliberately unnameable
  boundary that fails every `emission[...]` bound rather than passing it;
- **required-service emission edges** — `emit db.execute(...)` where `db` is a
  required `emission` service; the require key is a capability too.

A **capability-scoped** declaration (`emission[db]`) is an upper bound: a
provider may cross *less* (a purer body is admitted, and the projection still
speaks from the declaration), but crossing *more* — a different named boundary,
or the unnameable `*` reached through a first-class value — is refused. The
conservative direction is sound-by-design: honest higher-order code that only
ever routes *pure* functions through a dispatcher keeps compiling read-only
(the suite pins this so the bound cannot be tightened into a false positive).

## The G8 boundary is the review surface, not a wall

Some host reach is *legitimate* — a component genuinely must talk to the
outside world. The gate's job there is not to refuse it but to make it
**enumerable and reviewable**: every `extern` must carry a classification
(`pure`/`acquire`/`emission`, G8), and `revl audit` projects the per-component
**boundary surface** — every emission call site, the capabilities each may
cross, and the reachable host externs. `revl audit --diff` is the
**authority-drift gate** built on that surface: it keys on stable crossing
tokens (`emit:<comp>:<label>`, `host:<comp>:<extern>`) and **fails on
unacknowledged additions** — a new generation that widens what it reaches
outside the system must say so explicitly (docs/audit-diff.md).

A **teardown-position emission** (an emission reached from an `undo`/teardown
body) is the canonical "surfaced, not refused" case: calling the method
schedules it, so it is sound to admit — and it lands on the G8 audit surface
(`revl audit` lists the reached extern) rather than being rejected. The suite
asserts this positively.

## Non-goals (stated before they are discovered)

The gate defends the properties above. It deliberately does **not** defend the
following. Each is owned elsewhere; a non-goal silently mistaken for a
guarantee is exactly the failure this document exists to prevent.

1. **The gate does not sandbox host code.** An `extern emission fn` with an
   arbitrary `@py`/`@ts`/… body is arbitrary host code **by design** — it is
   the language's escape hatch. The gate's contract is to *surface* it on the
   G8 audit review surface (classified, enumerated, diffable), **not** to
   confine what it does. `extern emission fn rm() = @py { os.system(...) }`
   compiles, is flagged `readOnlyHint: false` / `destructiveHint: true`, and
   appears as a `host:` crossing — and it runs whatever it wants. Sandboxing,
   quarantine, and capability *revocation* at deploy time are the **quarantine
   tier's** job (roadmap item 45), not the admission gate's. The gate answers
   "is this reach declared and on the surface?"; item 45 answers "is this reach
   allowed to actually happen?".

2. **The gate defends the declaration, not the runtime.** Guarantees are
   compile-time verdicts over the *lowered composition*. Where a *runtime* on a
   particular tier diverges from the contract, that is a fenced runtime
   divergence, not a gate defence — e.g. the cordis-rs A1 divert-at-boundary
   difference, the cordis4j equal-realm-string separation (G2 holds at the gate;
   the Java *runtime* separates equal labels), and the cordis-TS `assertActive`
   residue (G5's runtime concern). All are recorded in docs/contract-errata.md
   with trigger and blast radius. The gate refuses the equal-realm *second
   provider* (G2) at compile time on every tier; it cannot make a runtime honour
   the resolution it proved.

3. **The gate is sound where types are known and silent where they are not.**
   The gradual-typing frontier — host-object *results*, un-annotated arrow
   *values* — is a set of **fenced** typing holes (docs/contract-errata.md,
   "Typing gaps"), not gate defences. Crucially these are *typing* holes, not
   *authority* holes: an arrow value cannot launder an **emission** past G4 (the
   emission fixed point propagates `*` through first-class references regardless
   of whether the arrow is typed), so a read-only lie is still impossible even
   where the arrow's arity is unchecked.

4. **Enumeration completeness of the G8 surface has one fenced gap.** A host
   block reached **only** through a first-class function value is correctly
   flagged non-read-only at the MCP tool layer (the G4 defence holds), but is
   **not** enumerated in `revl audit`'s per-component `externs` list, so it
   produces no `host:` crossing token and is invisible to `revl audit --diff`.
   This does not let an operation lie about being read-only; it lets a
   *bare-`emission`* operation (declared capability already `*`) widen its
   concrete host reach without the authority-drift gate noticing. Recorded in
   docs/contract-errata.md ("G8 enumeration is incomplete for first-class host
   reaches") and pinned `xfail` in the suite; the fix belongs in the boundary
   computation (`src/revl/__main__.py` `_boundary`), reusing the same first-class
   reachability the G4 fixed point already computes.

## The soundness exit test, offensively

The roadmap's soundness exit test is: *no reachable program can violate a
guarantee without either a compile error or a documented errata entry.* This
threat model plus `tests/test_adversarial_gate.py` is the offensive proof of
that test: each attack either (a) is **refused** with the guarantee-naming
diagnostic, (b) is **surfaced** on the G8 audit review surface, or (c) is a
**documented gap** — pinned `xfail` with a reason and fenced in
docs/contract-errata.md. An attack that silently succeeds with none of the
three is the bug this suite exists to catch.
