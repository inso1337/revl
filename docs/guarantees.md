# The guarantees and rules at a glance

Every refusal revl prints names the code it enforces (the `(G4)` in the
message). Those codes are cited all over the roadmap, the README, and the
threat model, and defined in prose in several places. This page is the one
decode table: for each code, a one-line meaning, where the compiler enforces
it, and its ORIGIN, meaning whether it comes from the Cordis paradigm (an
inherited hypothesis revl turns into a compile-time guarantee), is a
revl-original concept (revl's own, no paper anchor), or is standard type
theory / a named external proposal.

This is a glossary, not the detailed treatment. `docs/rejections.md` is the
per-code prose with reproducers (`examples/rejections/`), `DESIGN.md` section 4
is the checked-guarantees table with the paper anchors this page's origins are
read from, and `docs/threat-model.md` reads the same families as defences.
Where a definition or an origin could not be sourced from an in-repo doc it is
marked "see <doc>" or "origin: unconfirmed", never guessed.

Enforcement sites use the same vocabulary as `docs/rejections.md`: `parser` /
`checker` (per-file frontend), `lower` (per-component lowering, includes the
taint and emission flow analyses), `linker` (whole-composition link), and "by
construction" (no violating program is expressible).

## Gate guarantees (G1 to G9)

Origins are quoted from the `DESIGN.md` section 4 table ("Checked guarantees"),
whose "Paper anchor" column is the primary origin source. G1 to G8 each realize
a Cordis-paper definition or theorem, made a compile-time guarantee by revl. G9
is the one revl-original member: the roadmap and threat model both note it has
no paper anchor.

| Code | Guarantee (one line) | Enforced at | Origin |
| ---- | -------------------- | ----------- | ------ |
| [G1](rejections.md#g1--declared-access) | declared access: a component reads only what it requires | lower (name-scoped) | Cordis paradigm (paper Def. 25) |
| [G2](rejections.md#g2--provision-disjointness) | provision disjointness: one provider per key (per realm) | linker | Cordis paradigm (paper Def. 43) |
| [G3](rejections.md#g3--acyclic-dependencies) | acyclic dependencies: a cycle can never activate | linker | Cordis paradigm (paper section 6.5) |
| [G4](rejections.md#g4--inverse-or-emit) | every mutation carries an inverse, or admits irreversibility with `emit` | lower (emission fixed point) | Cordis paradigm (paper Def. 8, section 6.1) |
| [G5](rejections.md#g5--teardown-cannot-register-effects) | teardown cannot register effects | by construction (grammar) | Cordis paradigm lifecycle-reentrancy hardening (DESIGN.md anchor "DS mod 6 / PR #39") |
| [G6](rejections.md#g6--purity-outside-effect-forms) | purity outside effect forms | parser / checker | Cordis paradigm (paper Def. 48, confinement) |
| [G7](rejections.md#g7--derived-lifo-teardown) | derived LIFO teardown | by lowering (+ totality check) | Cordis paradigm (paper Thm. 16) |
| [G8](rejections.md#g8--the-boundary-surface-is-enumerable) | the boundary surface is enumerable | parser (extern classification) + `revl audit` | Cordis paradigm (paper section 6.1) |
| G9 | untrusted data cannot create authority without a declared declassification | lower (taint flow) | revl-original (no paper anchor; roadmap item 249) |

## Ordering and async rules (the A family)

The A family is the lifecycle discipline: `DESIGN.md` pillar 4 states the
operational semantics follow the paper's section 4.3 rules, so most A rules are
paradigm-derived, but only A1 and A8 carry an explicit paper anchor in the
source docs. A4 and A7 are not assigned. Definitions are quoted from the
`docs/rejections.md` "families" table.

| Code | Rule (one line) | Enforced at | Origin |
| ---- | --------------- | ----------- | ------ |
| [A1](rejections.md#a1--iteration-boundaries-exist-only-during-activation) | iteration boundaries exist only during activation (`await` only in a component body) | lower | Cordis paradigm (paper section 4.3.2, cited in rejections.md#a1) |
| [A2](rejections.md#a2--no-acquisition-after-a-provision) | no acquisition after a provision | linker | Cordis paradigm lifecycle ordering (LIFO + withdrawal guard); explicit paper anchor unconfirmed |
| [A3](rejections.md#a3--host-safe-identifiers) | host-safe identifiers (renames, never refuses) | lowering transform | revl-original (lowering rename transform) |
| [A5](rejections.md#a5--compensation-accompanies-an-emission) | compensation accompanies an emission | by construction | revl emission/compensation design (DESIGN.md section 3.5); origin unconfirmed |
| [A6](rejections.md#a6--provide-methods-match-the-service-signature) | provide-methods match the service signature | lower / compat gate | other (standard signature checking, revl service model); origin unconfirmed |
| [A8](rejections.md#a8--mid-body-failure-reverts-and-contains) | mid-body failure reverts and contains (L-Raise) | lower (+ runtime) | Cordis paradigm (L-Raise failure transition) |
| A9 | a provide key is declared in the component's `provides` clause | lower | origin: unconfirmed (declared-provision rule; see rejections.md "families") |

## Type-layer rejections (T1 to T4)

The typing rules. `docs/threat-model.md`, `docs/guide-humans.md`, and
`docs/stability.md` all cite these as "T1 to T3"; T4 is the more recent
erased-field-read rule (its reproducer is tagged `T1` in the diagnostic
stream). See the T-prefix note at the end: T here means the type layer, not a
tier.

| Code | Rejection (one line) | Enforced at | Origin |
| ---- | -------------------- | ----------- | ------ |
| [T1](rejections.md#t1--declared-types-are-checked) | declared types are checked (arity, args, returns, exhaustiveness, fields) | checker | other (standard static type checking) |
| [T2](rejections.md#t2--absence-is-optt) | absence is `Opt[T]`; `null` has no type | checker | other (option types / null-free; revl design choice, syntax-2.0 section 2) |
| [T3](rejections.md#t3--a-hole-is-an-obligation) | a hole is an obligation: it checks, but never runs (admission refuses open holes) | admission gate | revl-original (holes, docs/holes.md) |
| [T4](rejections.md#t4--no-field-read-off-an-erased-value-any--value) | no field read off an erased value (`Any` / `Value`) | checker (frontend) | revl-original (roadmap item 380) |

## Confidentiality and taint (qualifiers, G9, Secret families)

`src/revl/taint.py` (roadmap item 249, Slice A) is the canonical source. The
system is a set of TYPE QUALIFIERS orthogonal to the base type, not a numbered
tier ladder: there is no "taint tier T-number" on main (see the T-prefix note).
The lattice is the powerset of origin labels; the enforced refusal is G9, and
its confidentiality siblings are the `Secret` families. `docs/threat-model.md`
groups "the `Secret` families" with G9 as the same no-paper-anchor family.

| Code | Meaning (one line) | Enforced at | Origin |
| ---- | ------------------ | ----------- | ------ |
| `Untrusted[T]` | value returned across an untrusted-origin boundary; joins by set union | qualifier (checker side-table, taint flow at lower) | revl-original (item 249) |
| `Trusted[T]` | an authority-granting sink parameter; an `Untrusted[T]` may not reach it without a declassifier | qualifier (lower, taint flow) | revl-original (item 249) |
| `Secret[T]` | a capability-bound secret, redacted at boundaries (config trace, WAL, seam text, approval tickets) | qualifier (lower, taint flow) | revl-original (items 249 / 256) |
| `Retained[T, P]` | a value held under retention policy `P`: a deadline, a residence, a legal-hold exception, the principals that may request deletion, and which derivative classes the policy covers | qualifier (lower, taint: declaration and flow) | revl-original (item 472) |
| G9 | untrusted data cannot create authority without a declared declassification (`endorse`, or a `verified` checked parser) | lower (taint flow) | revl-original (no paper anchor; roadmap item 249) |
| [G-SECRET](rejections.md#the-families) | a capability-bound secret never leaves its capability's own extern bodies through any revl construct or declared crossing | lower (taint flow) | revl-original (items 249 / 256); no paper anchor |
| [G-SECRET-FLOW](rejections.md#the-families) | a `Secret[T]` value never reaches a disclosure sink; it crosses only at a declared `Secret[T]` receiver and downgrades only at a declared `endorse[confidential]` | lower (taint flow) | revl-original (items 249 / 256); no paper anchor |
| [G-RETAIN](rejections.md#the-families) | a `Retained[T, P]` value past `P`'s retention deadline never reaches a persistence sink (a `db`/`fs`/`store`/`kv`/`blob`/`archive`/`index`/`cache`/`queue`/`wal` crossing), unless `P` declares a legal hold, which overrides the deadline | lower (taint: declaration and flow) | revl-original (item 472); no paper anchor |

A declared receiver is not a licence to RECORD. A `Secret[T]` declaration
authorises disclosure to the receiver it names; it says nothing about a durable
copy taken beside the call. The one place that matters for a witnessed extern is
its discharge descriptor: the inverse is reconstructed from the `Ok` witness, so
the witness is the field a crash log keeps, and an author who declared the
witness confidential — on either end of the call, `Result[Secret[W], E]` on the
extern or `Secret[W]` on the inverse's parameter — gets the placeholder there
too. See `docs/design/243-witnessed-externs.md` rule 4.

## Sandbox isolation rungs

From `docs/design/411-sandbox-placement.md` (item 411). Isolation is a
per-process placement dimension; the `isolation` manifest key picks a rung. The
rungs are NAMED (`wasm-cell`, `container`, `microvm`). Some `sandbox_runtime.py`
comments and issue #107 informally tag the `microvm` rung "T6" (its position on
the seam-tier ladder), which is prose, not a formal code, and the one "T"
look-alike outside the type layer (see the T-prefix note). Origin is revl-original placement design; the `wasm-cell` rung
is the paradigm's physical-confinement idea (DESIGN.md section 8) realized as a
placement citizen.

| Rung | Boundary (one line) | Enforced at | Origin |
| ---- | ------------------- | ----------- | ------ |
| `wasm-cell` | wasm instantiation inside a py host process; confinement is a generated import set (no fs/net envelope) | plan-time gate + wasmtime instantiation | revl-original (item 411; realizes DESIGN.md section 8 wasm confinement) |
| `container` | OS namespaces, shared kernel; kernel-enforced fs/net/pid isolation | plan-time gate + isolation runtime (docker/podman) | revl-original (item 411) |
| `microvm` | own kernel under a VM monitor; strongest OS-level boundary | plan-time gate + VM monitor | revl-original (item 411; largely staged, not fully landed) |

The plan-time capability gate over `[sandbox.needs]` is ADVISORY (an unverified
author claim, G8 keeps host bodies opaque); the envelope, not the gate, is the
security boundary on the container and microVM rungs. See the 411 doc's
trust-boundary section.

## Exit-test codes (E1 to E9) and the operator E-Stop

Two unrelated things wear an "E". Neither is a guarantee code.

**Exit-test codes (E1, E2, ...).** In the roadmap and in individual design
notes, `En` is an EXIT TEST, a numbered acceptance criterion, and the numbering
is PER DOCUMENT (each design doc restarts at E1). The most-cited space is the
v3.0 release milestone in `docs/v2.0-roadmap.md` (its preamble, E1 to E9): for
example E1 is "six-tier conformance green", E3 is "the live-systems demo runs
from a tagged clean checkout". `docs/conformance.md` calls a real emit gap "the
E1 exit-test signal". So a bare "E3" decodes only against the document that
cites it. Origin: revl project process (release/exit criteria), not a paradigm
or type-theory concept.

**The operator E-Stop** (`docs/design/443-estop.md`, item 443) is a runtime
feature, an emergency HALT verdict, not an E-numbered family. It adds a third
`Verdict` constructor (`halted`) alongside `commit` and `abort`; it strands
every registered entry rather than replaying or discharging, trading resource
cleanliness for stop latency. It is honored only on the py reference tier today
(`revl.estop.TIERS_WITH_ESTOP == {"py"}`); on the other five tiers a placement
halt is a SIGKILL reported UNKNOWN. Origin: revl-original (operator feature,
item 443). It does NOT define an E1 to E8 tier ladder: there is no E-Stop tier
numbering on main, so a bare "E-Stop tier" reference elsewhere means the honor
table above (which tiers run the halt), not an `E`-numbered family.

## The "T" prefix is one space, and two look-alikes

A reader who hits a "T" code should read it as the TYPE LAYER. Across the
shipped docs (`docs/threat-model.md` line 38, `docs/guide-humans.md`,
`docs/stability.md`, `docs/rejections.md`), a `T`-number always means a
type-layer typing rejection, T1 to T4 above. Two things that are NOT T-codes,
kept separate here so nobody conflates them:

- **Runtime tiers** are the six backend runtimes (`reference`/cordis-py,
  cordis-TS, cordis-wasm, cordis-rs, cordis4j, cordis-go; `docs/vision.md`).
  Prose calls them "tiers" in lowercase; they are named, not T-numbered.
- **Taint qualifiers and sandbox rungs** are named, not T-numbered on main
  (`Untrusted`/`Trusted`/`Secret`; `wasm-cell`/`container`/`microvm`). The one look-alike is the `microvm` rung being informally tagged "T6" (a seam-tier
  ladder position) in some `sandbox_runtime.py` comments and in issue #107; that is
  prose, not a formal `T` code. No taint-tier or seam-tier `T`-number family exists
  on main, so this page treats the `T` prefix as the type layer only.
