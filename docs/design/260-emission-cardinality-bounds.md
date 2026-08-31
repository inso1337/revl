# Emission cardinality bounds - cost ceilings proved, not observed (item 260)

**Status: design, not implemented.** This document specifies (a) a static
per-capability crossing-count ceiling per activation, reported on the audit
surface next to the capability table, with an honest `unbounded` verdict; and
(b) resource budgets on emissions, statically checked where the shape allows,
runtime-enforced otherwise, carried in the attestation, and attenuated on
delegation. It changes no existing program: every field is additive and absent
unless a component crosses a boundary, exactly as item 309 and item 373 added
their fields.

The distinction the feature draws: runtime cost accounting (a session ledger)
says what an activation *did* spend. Cardinality bounds say what an activation
*can* spend, read off the IR before anything runs. A runaway-loop bug that
today surfaces as a surprising invoice becomes a compile-visible property.

## 0. What already exists (the seams this lands on)

Grounding, so the design lands on real code and does not reinvent machinery.

- **The crossing walk.** `__main__._boundary(ir)` walks each component body
  (`walk_steps` / `walk_expr`) and collects, per component, a `stats` dict:
  `emissions` (the SET of `key.method` labels), `capabilities` (label ->
  declared scope list), `compensated`, `awaits`, and the reached `externs`.
  This is a SET membership computation - it answers "which boundaries", never
  "how many times". Cardinality adds the count alongside it.
- **The capability fixed point.** `emission_analysis._emitting_capabilities`
  is a least fixed point over the static fn call graph: name -> the SET of
  capabilities its call reaches, closing through chains of `fn`s, with the
  first-class-value escape hatch adding `*` (the unnameable capability) when a
  fn hands an emitting callable to a dispatcher. Cardinality reuses the same
  call graph but lifts the lattice from sets to count vectors.
- **The honest verdict pattern.** `distribute.distributability(ir)` returns,
  per service, `{"verdict": "transport-safe" | "address-space-bound",
  "reasons": [...], ...}`. `unbounded` mirrors this exactly: a named verdict
  with a machine-readable reason, never a silent omission.
- **The attenuation algebra.** `cap_order` already defines capabilities as a
  partial order `(T, P)` with a `covers` relation, and ALREADY carries
  ceiling-kind parameters (`calls`, `size`) with the order "a smaller ceiling
  is narrower" (`_param_leq`, `order == "ceiling": narrow <= wide`).
  `_check_spawn_attenuation` in `lower.py` uses `covers_set(held, reach)` to
  refuse a child whose reach is not covered by the spawner's held set. Budget
  attenuation is not new machinery; it is budgets riding the ceiling params
  that `covers` already orders.
- **Recursion detection.** `lower._fn_call_graph` + `lower._reaches_self`
  already find direct and mutual recursion (used by `_check_verified_totality`).
  The count analysis reuses them to decide boundable vs looping.
- **No `const`.** `lower.py` line ~161 records "revl has no `const`". A
  statically-known iteration bound therefore comes from an integer LITERAL or a
  component `config` field (whose value binds at composition, not at
  component-compile). This shapes the concrete-vs-symbolic ceiling rule in §2.

## 1. The audit-surface shape

Cardinality is a new key `cardinality` on each component's boundary entry,
computed next to `emissions`/`capabilities` and rendered next to the
capability line. It is present only when the component has crossings (matching
the existing `if stats["emissions"] or stats["awaits"] or host:` render gate),
so a crossing-free component's audit is byte-identical to today.

### 1.1 JSON (`revl audit --json`, and `audit_diff.audit_report`)

Per component, under `boundary[<component>]`:

```json
"cardinality": {
  "per_capability": {
    "model": {"bound": 3, "kind": "bounded"},
    "bus":   {"bound": 1, "kind": "bounded"}
  },
  "verdict": "bounded"
}
```

An unbounded component:

```json
"cardinality": {
  "per_capability": {
    "model": {"bound": null, "kind": "unbounded",
              "reason": "recursion through `poll_forever` has no decreasing bound"}
  },
  "verdict": "unbounded"
}
```

A component whose ceiling is symbolic in a config field (the harness's
`run_loop` seeded by `config.max_steps`, before composition pins it):

```json
"cardinality": {
  "per_capability": {
    "model": {"bound": null, "kind": "bounded-symbolic",
              "expr": "config.max_steps", "per_iter": 1}
  },
  "verdict": "bounded-symbolic"
}
```

`verdict` is the component roll-up: `bounded` when every capability is
`bounded`; `bounded-symbolic` when some are symbolic and none are unbounded;
`unbounded` when any capability is `unbounded` (unbounded dominates - the honest
worst case wins, never averaged away). Keys are capability tokens (the same
tokens the `capabilities` line uses), so a policy can join the two surfaces on
one key.

### 1.2 Text (`revl audit`)

One line under the existing `capabilities:` line in the `boundary:` detail:

```
component Agent  (agent.rvl)
  requires: model, tools
  provides: agent
  boundary: emissions: model.complete [model], tools.call [tools] (0 compensated);
            capabilities: model, tools;
            cardinality: model <= 3 per activation, tools <= 3 per activation
```

Unbounded is loud, on its own clause, never folded into a comma list:

```
  boundary: emissions: model.complete [model] (0 compensated);
            capabilities: model;
            cardinality: model UNBOUNDED (recursion through `poll_forever`
            has no decreasing bound)
```

Symbolic:

```
            cardinality: model <= config.max_steps per activation (1 per iteration)
```

The roadmap's example spelling ("model.complete: <= 3 per activation; send.*:
<= 1") is preserved: the token is the capability, the number is the per-activation
ceiling, and a wildcard capability (`send.*`) is a capability token like any
other. We report per CAPABILITY (the boundary), not per emission label, because
the ceiling that a budget and a policy care about is "how many times through
`model`", and two labels sharing a capability sum into it.

## 2. The counting rules

Let a body's crossings be attributed to capability tokens exactly as the audit
already does: a req-keyed emission counts against its required key (resolved to
the method's `emission[...]` scope where declared), a host emission or
first-class dispatch counts against `*`. The question cardinality answers is the
MULTIPLICITY of each, per activation.

### 2.1 Non-looping bodies (the exact count)

A body with no recursion in its reachable fn call graph has a finite, exact
per-capability count read structurally off the step list:

- A straight-line sequence of steps sums the counts of its steps.
- An `emit` step contributes 1 to each capability its emission crosses
  (multiple caps when the method declares `emission[a, b]`; each is +1).
- A `match` / `if` contributes the MAX over its arms (the worst arm is the
  ceiling; a proved upper bound must hold on every path, so we take the
  branch that spends the most, never the sum and never an average).
- A call to a pure `fn` contributes that fn's own per-capability count vector
  (computed once, memoized), because a fn call runs its body once.
- A call THROUGH a first-class arrow parameter that carries emissions
  contributes `*` at multiplicity 1 per syntactic call site, and marks the
  capability set with `*` (we can count the call sites, but not name the
  boundary; see §5.1 for why this does not let a body under-report).

This is a bottom-up evaluation over the call graph condensation (SCCs). A
non-looping body is one whose reachable SCCs are all singletons with no
self-edge; the count vector is then well defined and exact.

### 2.2 Bounded-iteration bodies (the `max_steps` shape)

The expressible-iteration design (`docs/expressible-iteration.md`) made the
agent loop writable as bounded structural recursion through a pure fn that is
handed an emitting arrow:

```revl
fn run_loop(msgs: List[Msg], step: (List[Msg]) -> Step, n: Int) -> Step {
  if (n <= 0) { return Final("max_steps exhausted") }
  return match step(msgs) {
    Final(answer) => Final(answer),
    NeedTool(req) => run_loop(msgs + [req.result], step, n - 1),
  }
}
```

A self-recursive fn `f` is a BOUNDED ITERATION when all of the following hold
(the recognizer is deliberately narrow; anything it cannot certify is
`unbounded`, never optimistically boundable):

1. **A fuel parameter.** There is an integer parameter `n` of `f` such that
   every recursive call to any member of `f`'s recursion SCC passes, in `n`'s
   position, an expression of the form `n - k` where `k` is a positive integer
   literal (`k >= 1`). No call may pass `n`, `n + _`, `n * _`, or a value not
   derived from `n` by subtracting a positive literal.
2. **A dominating base guard.** Every path to a recursive call is dominated by
   a guard `if (n <= c) { <base> }` (or `n < c`), `c` an integer literal, whose
   `<base>` branch contains no call into the SCC. This is the decreasing measure
   with a floor; it is the same "structurally smaller value or a syntactically
   bounded loop" shape `_check_verified_totality` already names.
3. **A statically-resolvable initial fuel.** At the site that first enters the
   loop (the provide-method call `run_loop(..., config.max_steps)`), the fuel
   argument reduces to either an integer LITERAL `N0` or a component `config`
   field. Nothing else (a method parameter, a host-returned value, an
   arithmetic expression over those) is statically bounded.

When (1)-(3) hold, the max iteration count is `ceil((N0 - c) / k)` (with a
LITERAL `N0`), and the per-activation per-capability ceiling is:

```
ceiling(cap) = base_crossings(cap) + max_iters * per_iter_crossings(cap)
```

where `per_iter_crossings(cap)` is the §2.1 count of ONE traversal of the
recursive arm excluding the recursive self-call, and `base_crossings(cap)` is
the §2.1 count of the base branch. The arrow `step` passed into the loop is the
emitting callable; its own per-capability count folds in through the
first-class-value edge (§5.1), so `step = msgs2 => emit model.complete(msgs2)`
contributes 1 to `model` per iteration and the loop reports `model <= N0`.

When the fuel is a `config` field rather than a literal, `N0` is symbolic: the
component-level verdict is `bounded-symbolic` and the ceiling is reported as
`config.max_steps` (with `per_iter`), because a component is compiled before its
config is bound. **The symbolic ceiling resolves to a concrete integer at
composition** (§2.4): the manifest binds `config.max_steps` to a literal, and
`revl audit` on the composed manifest substitutes it, yielding `model <= 8`.
This two-level answer is deliberate and honest: the component ships a proof
schema; the composition instantiates it.

### 2.3 When a body is `unbounded` (the precise definition)

A capability is `unbounded` for a component when its reachable call graph
contains a recursion SCC that reaches an emission of that capability AND the SCC
is not certified as a bounded iteration by §2.2. Concretely, `unbounded` fires
on:

- recursion (direct or mutual) with no fuel parameter, or a fuel that does not
  strictly decrease by a positive literal on every back-edge;
- a bounded-SHAPED recursion whose initial fuel is not a literal or config
  field (a host-returned or parameter-derived `N0`) - the shape is right but
  the ceiling is not provable, so we refuse to invent one;
- an emission reached only through a first-class arrow whose provenance crosses
  a recursion SCC we cannot bound (the `*` capability inherits the SCC's
  unboundedness);
- an emission behind a host extern that itself loops (a host-driven loop) - the
  extern body is unchecked, so any multiplicity it hides is `*`-attributed and
  `unbounded` unless the extern declares a `calls` ceiling (§3), which caps it.

`unbounded` is a count verdict, not a set verdict: the component's capability
SET is still bounded (G4 is untouched, §4); it is the COUNT within a known
capability that is unproven. The verdict never disappears silently - it is a
required field of the roll-up, printed loudly, and the `--json` shape carries
`bound: null` with a `reason`, so a diff (item 261 / audit --diff) sees a
component GAINING an unbounded capability as a widening.

### 2.4 Composition-level resolution and policy input (item 33)

The per-component `cardinality` is a per-COMPONENT property. Item 33's
composition policy reads the roll-up over the whole manifest:

- `bounded-symbolic` ceilings resolve when the manifest pins the config field;
  an unpinned field stays symbolic and a policy may refuse it ("no symbolic
  model ceilings in this realm") or accept the schema.
- A policy gates on the verdict: `no component with unbounded model crossings`
  reads `cardinality.per_capability["model"].kind == "unbounded"` across
  components. This is the payoff the roadmap names, and it is a pure read of the
  field defined in §1.1 - no new analysis at the policy layer.

## 3. Resource budgets on emissions

The roadmap folds in external proposal #8: bound not just WHICH boundary but HOW
MUCH through it. The proposed spelling is

```
emission[network.call(host="api.x"), budget.requests=100, budget.bytes=10MB, budget.time=2s]
```

### 3.1 Grammar - reconciled onto the existing ceiling parameters

`cap_order` ALREADY parses ceiling parameters INSIDE a capability token
(`model.complete(calls=3)`, `fs.write(size=1024)`), already orders them, and
already erases them at grant mint into `remainingUses`. Rather than a parallel
`budget.*` namespace that `covers` would not understand, budgets ARE ceiling
parameters on the emission's capability token. The design adopts the
ceiling-parameter grammar as the canonical form and treats `budget.*` as sugar:

```
emission[network.call(host="api.x", requests=100, bytes="10MB", time="2s")]
```

parses (in `cap_order._REGISTRY`) to `Cap("network.call", {host, requests,
bytes, time})`. Three registry rows are added to the two that exist:

| param      | kind    | value order        | static? | notes |
|------------|---------|--------------------|---------|-------|
| `calls` / `requests` | ceiling | int, smaller narrower | STATIC (via §2) | `requests` is an alias of `calls` |
| `bytes` / `size`     | ceiling | int bytes, smaller narrower | runtime; static iff payload size is a compile-time constant | `"10MB"` canonicalizes to `10485760` |
| `time`               | ceiling | duration, smaller narrower | RUNTIME only | `"2s"` canonicalizes to a fixed unit (ms); wall-clock is not a static property |

Keeping `calls`/`requests` as one param is the load-bearing reconciliation: the
`calls` ceiling a capability declares and the cardinality bound §2 proves are
the SAME quantity, so the static check in §3.2 is exactly "proved-max <=
declared-calls".

### 3.2 Where each budget is checked

- **`requests`/`calls` - STATIC.** The §2 cardinality analysis proves a
  per-activation max for the capability. If the declared `calls` ceiling is
  present, the checker refuses a body whose proved-max exceeds it, with a G4
  diagnostic ("`Agent` may cross `model` up to 8 times per activation but its
  declaration caps `calls=3`"). If the body is `unbounded` for that capability,
  a declared finite `calls` ceiling is UNPROVABLE and is refused unless the loop
  is made bounded - this is the feature turning a runaway loop into a red
  compile. A runtime counter also enforces it (defense in depth, and the only
  enforcement for the `*`/host-extern case).
- **`bytes`/`size` - RUNTIME, static when constant.** Payload sizes are
  generally runtime values, so `size` is enforced by a runtime accumulator on
  the capability. When every payload on a path is a compile-time literal, the
  static pass MAY prove the sum and refuse over-budget at compile; absent that,
  it is a runtime ceiling carried in the attestation.
- **`time` - RUNTIME only.** Wall-clock has no static analogue; it is a runtime
  deadline on the capability, in the attestation, never a compile-time claim.

The attestation (the audit `--json` externs/boundary surface) carries every
declared budget verbatim as the capability's valuation, so a consumer reads the
enforced ceiling off the same document as the reach.

### 3.3 Attenuation on delegation

This composes with item 66 / item 294 for free, because budgets are ceiling
params and `_check_spawn_attenuation` already runs `covers_set(held, reach)`
over structured `(T, P)` caps. `covers` requires, per parameter the wider side
binds, `narrow <= wide`; for a ceiling that is `child_ceiling <=
parent_ceiling`. So a spawned child declaring `network.call(requests=10)` is
covered by a parent holding `network.call(requests=100)` (narrowing, admitted),
and a child declaring `requests=1000` is NOT covered (widening, refused with the
existing attenuation diagnostic). A child that DROPS the `requests` parameter is
also refused, because `covers`'s clause 2 treats a dropped parameter as widening
- so a child cannot escape a parent's budget by omitting it. No new fold: the
budget attenuation is the existing spawn attenuation reading three more registry
rows.

One addition is required in `_held_capabilities_pairs`: a parent's held ceiling
must be its DECLARED ceiling MINUS what the parent itself already spends before
the spawn, so a parent that will itself make 90 of its 100 requests cannot hand
a child a 100-request budget. The conservative first cut (§6, Slice 3) holds the
full declared ceiling and defers the spend-aware subtraction; it is called out
in §5.3 as the sharpest correctness question.

## 4. G-invariant interaction - it SHARPENS G4, never weakens it

G4 today (`docs/capabilities.md`): a service declaration is an UPPER BOUND on
every provider - a provider's body may not cross a boundary the declaration does
not name. That is a bound on the SET of capabilities. Cardinality and budgets
add a bound on the COUNT within each already-bounded capability. Formally:

- Before: `reach(provider) subset-of declared(service)` (set containment).
- After: additionally, for each cap `c`, `count(provider, c) <=
  ceiling(declared, c)` when a ceiling is declared, and the audit REPORTS
  `count(provider, c)` whether or not a ceiling is declared.

This is strictly more information on the same axis; it cannot admit a program
G4 rejects (the set check runs unchanged and first), and it can reject a program
G4 admits (a body within its capability set but over its declared `calls`
ceiling, or an unbounded body under a declared finite ceiling). The `unbounded`
verdict weakens nothing: it is the honest statement that the count bound is not
provable, printed loudly, exactly as a bare `emission` today honestly declares
"any capability". A component with an unbounded count still has its full G4 set
bound. G4's own diagnostic code is reused for the ceiling-violation refusal, so
the sharpening reads as G4, not a new invariant bolted alongside it.

## 5. Adversarial self-review

Three (plus) attacks on this design, each with a mitigation or an OPEN mark.

### 5.1 Can a body under-report by hiding crossings behind a first-class callable or an extern?

**Attack.** A body passes an emitting arrow into a dispatcher, or reaches an
emission through a host extern, so the syntactic step-list count misses the real
multiplicity: `dispatch(f)` where `f` emits, or a host extern that internally
loops. If cardinality counted only visible `emit` steps it would report `model
<= 0` for a body that actually crosses `model` many times.

**Mitigation.** The count analysis MUST reuse the exact reach the existing
`_emitting_capabilities` / `_boundary` fixed point computes, including its
first-class `*` widening, and MUST NOT count only `emit` steps. A first-class
call site whose value carries emissions counts against `*` at multiplicity
"unknown", which promotes the capability to `unbounded` unless the dispatcher is
itself a bounded §2.2 loop with a known call count. A host emission extern
contributes `*` and is `unbounded` for `*` unless it declares a `calls` ceiling
(§3.2), which caps it by DECLARATION (the extern body is unchecked, so the
ceiling is a runtime-enforced promise, and the audit says so). The key rule:
**an unnameable or unbounded reach is never counted as zero; it is counted as
`*`/unbounded.** This is the same soundness the boundary surface already relies
on, so cardinality inherits it rather than re-deriving it. This is the attack
every prior design review found a CRITICAL in, and the mitigation is to refuse
to invent a finite count for any reach the set analysis already marks `*`.

### 5.2 Does bounded-iteration compose with recursion / mutual recursion?

**Attack.** Two mutually recursive fns `a -> b -> a`, or a bounded loop that
calls a second recursive fn, could let a bounded outer loop wrap an unbounded
inner one and be reported bounded; or a fuel that decreases on one back-edge but
not another could pass a naive per-call check.

**Mitigation.** The recognizer operates on the call-graph CONDENSATION (SCCs),
not individual calls. A recursion SCC is bounded ONLY when EVERY back-edge
inside it decreases the SAME fuel parameter by a positive literal and EVERY path
to any in-SCC call is dominated by the base guard (§2.2 (1),(2) quantify over
the whole SCC, not one call). A bounded outer loop that reaches a DIFFERENT
unbounded SCC inherits that SCC's `unbounded` for the capabilities it reaches
(unbounded dominates in the roll-up, §2.3). Mutual recursion with a shared
decreasing fuel is boundable; mutual recursion where the fuel is threaded on
some edges but not others fails the "every back-edge" quantifier and is
`unbounded`. **OPEN:** the "same fuel parameter across a mutual-recursion SCC"
matching (identifying that `a`'s `n` and `b`'s `m` are the same measure) is
non-trivial; Slice 2 restricts certification to SINGLE-fn self-recursion (the
harness `run_loop` shape) and reports multi-fn SCCs as `unbounded`. This is
sound (never over-claims) but incomplete; widening it is a later slice.

### 5.3 Can a child budget escape attenuation?

**Attack.** A child declares a smaller `requests` but spawns a grandchild with a
larger one; or a parent hands out its full budget to two children that together
exceed it; or a child drops the `requests` param to escape the ceiling.

**Mitigation.** Dropping a param is already refused by `covers` clause 2 (a
dropped parameter reads as widening). Grandchild widening is caught because
attenuation is checked at EVERY spawn edge and the reach is closed over the
spawn graph (`_spawn_reached_surface_pairs` already closes transitively). The
per-child check is subset-of-parent, so two children each within the parent's
ceiling is admitted even if their SUM exceeds it - and this is CORRECT for a
per-activation ceiling ONLY IF the ceiling is per-activation-per-child, which it
is (each child is its own activation). **OPEN / sharpest finding:** the parent's
OWN spend is not subtracted from what it may hand down in Slice 3's first cut
(`_held_capabilities_pairs` holds the full declared ceiling). A parent declared
`requests=100` that itself makes 90 requests AND spawns a child with
`requests=100` is admitted, but the parent's realm now issues 190 where the
declaration reads 100. The per-activation semantics arguably make this sound (the
child is a distinct activation with its own 100), but if the intended reading is
a REALM-WIDE request budget, this is a hole. The design MUST pin the semantics
in Slice 1's spec text: **a `requests` ceiling is per-activation-per-component,
not realm-aggregate.** Under that reading there is no escape; if a later item
wants a realm-aggregate budget, that is a new ceiling kind (a `realm`-scoped
sum), not this one. Pinning this reading explicitly is what prevents a reviewer
reading it the other way and calling the admitted 190 a bug.

### 5.4 Is `unbounded` ever silently swallowed?

**Attack.** A roll-up that took the MIN or the average, or that omitted the
field when only some capabilities are unbounded, would hide an unbounded
capability behind bounded siblings.

**Mitigation.** The roll-up is MAX-flavored: `unbounded` dominates
`bounded-symbolic` dominates `bounded` (§2.3). The field is required whenever
the component crosses any boundary, and per-capability entries are never elided
- a bounded `bus` next to an unbounded `model` still prints both, with `model`
on its own loud clause. `--json` carries `bound: null` explicitly rather than
omitting the key, so a JSON consumer cannot mistake "unbounded" for "absent". A
crossing-free component omits the whole field (byte-identity), but such a
component has no capability to be unbounded, so nothing is hidden. **The one
residual risk** is a capability that the SET analysis misses entirely (then
cardinality never sees it to mark it unbounded) - but that is a soundness bug in
the boundary surface itself, not in cardinality, and §5.1 ties cardinality to
that same surface precisely so the two cannot disagree.

## 6. Implementation plan

Files and order, sliced so Slice 1 lands independently and is useful alone.

### Slice 1 - the static count and the audit field (landable alone)

- `src/revl/cardinality.py` (new): `cardinality(ir) -> {component -> {...}}`
  mirroring `distribute.distributability`'s shape and signature. Implements
  §2.1 (non-looping exact count) and §2.3's `unbounded` for any recursion SCC
  (WITHOUT §2.2 certification yet - every recursive body reports `unbounded` in
  Slice 1, which is sound). Reuses `lower._fn_call_graph` / `_reaches_self` and
  the `_emitting_capabilities` reach so §5.1 holds from day one.
- `src/revl/audit_diff.py`: add `"cardinality": cardinality(ir)` to
  `audit_report`. The additive-body test (`test_version_is_additive_body_unchanged`)
  stays green because both sides call `audit_report`; the schema file
  (`revl-interchange-v1.schema.json`) gains an optional `cardinality` member.
- `src/revl/__main__.py`: compute cardinality in `_boundary`'s per-component
  stats and render the text line under `capabilities:`; add it to the `--json`
  body next to `distributability`.
- `tests/`: a non-looping component asserts exact counts; a recursive component
  asserts `unbounded`; a crossing-free component asserts byte-identity of the
  audit. Golden updates for existing emitting compositions are expected and
  reviewed (like item 309's additions).

Slice 1 delivers the honest surface: exact counts for straight-line and
branching bodies, loud `unbounded` for every loop. That alone makes runaway
loops compile-visible.

### Slice 2 - bounded-iteration certification (§2.2)

- `src/revl/cardinality.py`: the fuel recognizer over single-fn self-recursion
  SCCs; concrete ceiling for a literal `N0`, `bounded-symbolic` for a config
  field. Multi-fn SCCs stay `unbounded` (§5.2 OPEN).
- `src/revl/__main__.py`: render `<= N per activation` and the symbolic form.
- Composition-level substitution of a manifest-pinned config field into the
  symbolic ceiling.
- `tests/`: the harness `run_loop` shape reports `model <= max_steps`, resolves
  to a literal once composed, and stays sound on a non-decreasing fuel.

### Slice 3 - budgets (§3)

- `src/revl/cap_order.py`: three registry rows (`requests` alias of `calls`,
  `bytes`/`size` byte canonicalization, `time` duration order); `covers`
  already handles them.
- `src/revl/lower.py`: the G4 static refusal when a proved-max exceeds a
  declared `calls`/`requests` ceiling, and when an `unbounded` capability
  carries a finite declared ceiling; the runtime counters for `size`/`time` and
  the `*`/host case; `_held_capabilities_pairs` reads the new ceilings into the
  spawn attenuation (holding the full declared ceiling - the §5.3 conservative
  cut, with the per-activation-per-component semantics pinned in the doc).
- backends: the runtime accumulators for `size`/`time`/`requests` on the
  capability, in the attestation.
- `tests/`: static over-budget refused; child budget narrower admitted, wider
  and dropped-param refused (attenuation); the attestation carries the ceilings.

Slices 2 and 3 are independent of each other and both build on Slice 1's count
vector. Slice 3's static `requests` check consumes Slice 2's certified ceiling,
so `requests` is fully static only once Slice 2 lands; before that a declared
`requests` ceiling over a Slice-1-`unbounded` body is refused (sound, and the
right default).

## 7. Exit criteria

1. A straight-line and a branching (match/if) component report exact
   per-capability counts on `revl audit` and `--json`; the branch count is the
   max over arms, not the sum.
2. A recursive component reports `unbounded` loudly with a reason, on both
   surfaces, and an `audit --diff` sees a newly-unbounded capability as a
   widening.
3. The harness `run_loop` shape (Slice 2) reports `model <= config.max_steps`
   per activation, resolving to a concrete integer when the manifest pins the
   config.
4. A body whose only crossing is behind a first-class arrow or a host extern is
   NEVER reported as a finite count it cannot prove; it is `*`/unbounded (the
   §5.1 soundness tie).
5. A component with no crossings has a byte-identical audit to today (field
   absent).
6. (Slice 3) A declared `requests` ceiling below a proved-max is a red compile
   with a G4 diagnostic; a spawned child's budget wider than its parent's is
   refused by the existing attenuation check; the attestation carries every
   declared budget.
7. G4's set check is untouched: no program G4 admits today is rejected by the
   set analysis, and cardinality only ADDS count-axis refusals.
