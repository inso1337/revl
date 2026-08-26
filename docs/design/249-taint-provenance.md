# Design: taint and provenance (item 249)

Status: design (this document). No code lands with it. Implementation in slices,
sequenced below.

## What 249 is, in one line

Prompt-injection defence at the level of *values*: untrusted data cannot
**directly** create authority. An injected instruction that arrives as content
(a web page, a tool result, an LLM completion) can be read, summarized, and
reasoned over, but it cannot become a shell command, a capability name, a policy
edit, or an unratified outbound send without passing an explicit, audited
declassification step first. This is the information-flow companion to
[prompt-injection-resistance.md](../prompt-injection-resistance.md): confinement
(G1/G4/G8 + the item-33 policy) bounds *what boundaries a component may reach*;
taint bounds *what a value may flow into once it is inside*.

It closes residual risk #1 of that document, stated there and left open:

> Injection that stays within already-granted capabilities. If a component is
> legitimately granted `emission[db]` and an injected instruction makes it write
> the wrong thing through `db`, that write is in-bounds. Confinement caps what
> boundaries a component can touch, not what it does within them.

Taint is how "the wrong thing" becomes checkable: the wrong thing is
*untrusted content flowing into a sink that grants authority*, and that flow is
exactly what an information-flow analysis names.

## The two mechanisms this item folds, and how they reconcile

The roadmap item carries two proposals that were never reconciled:

1. **Dynamic runtime taint** (main text). A value that returns across a boundary
   is tagged at runtime with its origin (`web:example.com`, `model`,
   `fs:workspace`); the tag propagates through pure computation; a tagged value
   reaching another emission's arguments is a checked runtime event.
2. **Static input-trust labels** (external proposal #10). Types `Untrusted[Str]`
   and `Trusted[Policy]`; the checker refuses `Untrusted[Str]` into a shell
   command, a capability name, or a policy update unless a sanitizer or approval
   step intervenes.

They are not competitors. They are the **compile-time half and the runtime half
of one information-flow discipline**, and revl already ships that exact
two-halves shape elsewhere: the emission analysis is a static over-approximation
of reach ([emission_analysis.py](../../src/revl/emission_analysis.py)), and the
runtime is where the exact per-call crossing is recorded
([why_runtime.py](../../src/revl/why_runtime.py), exported to OTel by
[otel.py](../../src/revl/otel.py)). The `Int` bound is tracked the same way: a
static declaration plus a dynamic runtime check.

- **Static half** = the *checker*. It proves, over the call graph, that no
  untrusted value can reach a refusal sink without a declassifier on the path.
  Its verdict is a compile error, upstream of every runtime, and it is an
  over-approximation (it may label a value untrusted that a specific run would
  not). It needs **no runtime seam**.
- **Dynamic half** = the *observer and the last-mile gate*. It tags each runtime
  value with its precise origin, refines the static over-approximation to the
  exact set of origins a particular value actually carries, and drives per-value
  provenance onto the audit surface and OTel. It needs a runtime
  value-representation change on all six tiers.

The static half subsumes the safety claim; the dynamic half sharpens the
evidence and catches data-dependent origin that the static labels can only
coarsen. This is the reconciliation: **static is v1 and carries the security
guarantee; dynamic is v2 and carries the precision.** The rest of this document
makes each concrete.

## Decision 1: v1 is the static half. Rationale and sequencing.

**v1 = static `Untrusted[T]` / `Trusted[T]` labels, the refusal sinks, the
three declassifiers, and a static-over-approximate provenance on the audit
surface. The dynamic runtime taint is v2, queued behind item 243 Slice 2.**

Rationale:

- **The security property is a compile-time property.** "Untrusted input cannot
  directly create authority" is a statement about the *shape of the program*,
  not about a particular run. A static refusal is strictly stronger than a
  runtime tag check: it makes the dangerous program fail admission rather than
  fail at the moment the tainted value hits the sink. This is the same reason
  admission-is-install beats interpretation in the confinement story: the gate
  is upstream of every runtime.
- **The static half is buildable now.** It is a type-system and checker
  extension plus a fixed point over the call graph, and revl already has the
  fixed point: the taint propagation is the same monotone set-union closure that
  [`_emitting_capabilities`](../../src/revl/emission_analysis.py) walks for G4,
  seeded from origin labels instead of emission scopes. No backend changes, so
  all six tiers stay green (exactly the additivity argument 243 Slice 1 made).
- **The dynamic half edits a contended seam.** Tagging every runtime value with
  an origin is a change to the runtime value representation on all six tiers.
  That is the **same six-tier runtime seam item 243 Slice 2 is editing** (the
  per-backend emit and teardown loop that consumes witnessed inverses). Two
  work streams rewriting the same seam in parallel collide, so the runtime taint
  half **queues behind 243 Slice 2**, inherits its per-tier ownership split
  (wasm first-party, python a downstream fork, rust/typescript/java upstream),
  and inherits its Rust deferral behind item 278.

So the sequencing is:

```
now         : Slice A  (static, no runtime seam)          -- ships the guarantee
after 243 S2: Slice B  (dynamic runtime taint)            -- ships the precision
alongside   : Slice C  (256 secrets + 246/251 approval)   -- ties the ends off
```

The honest scope from the roadmap holds unchanged: taint is an
over-approximation in v1, the runtime tag makes it exact in v2, and the **G8
caveat applies** at both tiers. A lying extern classification lies about taint
too: if an extern that really reads the network is declared `pure`, its return
carries no origin and the analysis believes it. Taint origin is therefore
**declared per crossing** (like a capability scope, like a witnessed status),
never inferred from optimism. This is stated as a residual risk below, in the
same family as prompt-injection-resistance residual risks #2 and #3.

## Decision 2: the taint lattice and origin labels

### The lattice is the powerset of origin labels, ordered by inclusion

A value's taint is a **set of origin labels**. The order is subset inclusion,
the bottom is the empty set (fully trusted), and the join is set union. This is
deliberately the *same* lattice shape as the emission-capability set that
[`_emitting_capabilities`](../../src/revl/emission_analysis.py) computes, so the
propagation reuses the same least-fixed-point machinery over the same call
graph.

```
trusted            = {}                          (bottom)
web-tainted        = {web:example.com}
model-tainted      = {model}
web + model        = {web:example.com, model}    (a value derived from both)
secret-tainted     = {secret:openai_key}         (see Decision 4 / item 256)
```

`Trusted[T]` is the type of a value whose label set is empty. `Untrusted[T]` is
the type of a value whose label set is provably nonempty. In the static half the
checker tracks the *coarse* origin class it can prove (`web`, `model`, `net`,
`fs`, `input`, `secret`); the exact host in `web:example.com` is a runtime
refinement that the dynamic half fills in. This is intentional: the static label
is an over-approximation, the runtime tag is exact, and they agree because the
runtime origin set is always a subset of the statically declared one.

### Origin labels (the sources)

A label is minted at a boundary **crossing that returns a value into the
composition**, and the label is derived from the crossing's declared capability
scope, not guessed:

| Source | Label | Where it is declared |
| --- | --- | --- |
| external input at a boundary | `input` | a bare `emission`-return, or the generic ingress |
| a web fetch | `web:<host>` | an `emission[web]` extern's return |
| a network read | `net:<host>` | an `emission[net]` extern's return |
| an LLM completion | `model` | the model-boundary emission's return (item 257) |
| a filesystem read | `fs:<scope>` | an `fs`-scoped crossing's return (see below) |
| a provider secret | `secret:<name>` | item 256's capability-bound secret |

Trusted origins (empty label set): source literals, `config` values, the results
of the three declassifiers (Decision 3), and any value derived purely from
trusted values.

### Propagation

Propagation is monotone join along data flow, computed as the static fixed point
in v1 and as a runtime tag in v2:

- **Concatenation / interpolation.** `taint(a ++ b) = taint(a) ∪ taint(b)`. A
  trusted prefix does not launder an untrusted suffix.
- **Records are field-granular, not whole-record.** Constructing
  `{a: u, b: t}` where `u` is untrusted and `t` is trusted taints field `a` and
  leaves field `b` trusted; a read of `.b` is trusted. The static type is
  `Record{a: Untrusted[Str], b: Str}`. Whole-record tainting was rejected: it
  would make one untrusted field poison an entire config record and force
  spurious declassification. (Open question 3 flags the cost.)
- **Function calls.** Taint flows argument to result along the same call graph
  the emission fixed point already walks. A pure function that returns a value
  derived from an untrusted parameter returns untrusted; the checker infers this
  within a module and requires an explicit label at a *published service
  interface* (open question 6). This is the direct analogue of
  `_emitting_capabilities` propagating a capability through a chain of `fn`s.
- **Collections.** An element's taint joins into a read of the collection; the
  container itself is trusted metadata (length, presence) unless built from
  untrusted keys.

### Interaction with item 243's classification (decided explicitly)

Item 243 made one decision that this item inherits wholesale: a `witnessed`
extern **joins the same authority namespace as an emission**, carrying a
reversibility flag rather than forming a separate lattice
([243-witnessed-externs.md](243-witnessed-externs.md) surface note;
[emission_analysis.py](../../src/revl/emission_analysis.py) seeds the fixed point
with both `emission` and `witnessed` externs). Taint follows the identical rule:

- **Origin is derived from the crossing's capability scope, independent of
  classification.** A crossing scoped `[fs]` mints `fs:<scope>` on any content it
  returns, whether the extern is classified `emission` or `witnessed`. Taint does
  not care about reversibility; it cares about *where the bytes came from*.
- **A witnessed mutation's return is the `FsWitness`, which is host teardown data,
  not content.** Per 243 the witness flows only to the auto-registered inverse,
  and that inverse is required to be non-emission and non-witnessed. So the
  witness carries no useful taint (it never reaches a value-world sink), and we
  do not tag it. This falls out of 243's own rule, it is not a new exception.
- **A read-shaped `fs` crossing returns content tainted `fs:<scope>`.** When item
  244's `stdlib/fs.rvl` grows a read (a value-returning `fs` crossing, classified
  `emission` because a read is not a reversible mutation), its result is tainted
  `fs:workspace` exactly as a `web` fetch is tainted `web:<host>`. So the answer
  to "does a witnessed fs read taint its result" is: a witnessed *mutation* does
  not return content and taints nothing; a *read* taints its result with the
  scope label, and it does so because it is a boundary crossing, not because of
  its 243 classification.

The single rule, stated once: **taint origin = the capability scope of the
crossing that produced the value; the 243 classification decides reversibility,
never taint.** This keeps taint from forking the authority lattice that 243
deliberately kept singular.

## Decision 3: declassification (the sanitizer step)

A refusal sink accepts an untrusted value only when a **declassifier** sits on
the path. There are exactly three, chosen so that declassification is always
auditable and never silent. Each produces a `Trusted[T]` and each leaves a
record on the audit surface (Decision 5).

### 1. A checked parser (preferred: declassification by construction)

```
verified fn parse_int(s: Untrusted[Str]) -> Result[Trusted[Int], ParseError]
```

The untrusted bytes never reach the sink; a *validated structured value* does.
Because a `verified fn` must be total (G7), the parser cannot silently pass
malformed input through. This is the strongest form and the one the docs will
push first: an `Untrusted[Str]` becomes a `Trusted[Int]` (or a `Trusted[Policy]`,
etc.) only by surviving a total checker, and the failure branch is a typed
`Result`, not a smuggled string. It composes with item 257's typed model
boundary: a completion parsed into a `Trusted[AgentTurn]` is declassified by the
same rule.

### 2. An explicit `endorse` operator (the audited escape hatch)

```
let cmd: Trusted[Str] = endorse(user_line, capability = shell, reason = "...")
```

`endorse` is the deliberate downgrade for cases a parser cannot express. It is
**not** a silent cast. It is modelled as a first-class audited crossing:

- it requires a **declassification capability** (open question 5 recommends
  making it capability-scoped, `endorse ... capability = shell`), so the item-33
  boundary policy can forbid it per component or realm
  (`component Summarizer may not declassify web`);
- it emits a `declassify:<origin>:<component>` crossing token onto the G8 audit
  surface (Decision 5), so `revl audit --diff` treats a *newly added* endorse as
  a widening and fails, exactly as it fails on a new emission
  ([audit_diff.py](../../src/revl/audit_diff.py) `crossings`);
- it carries a `reason` string that lands in the audit record and the OTel event.

An `endorse` is therefore as reviewable as any other boundary crossing. It is the
honest escape hatch, on the surface, diffable, and policy-forbiddable.

### 3. An approval edge (human-in-the-loop, ties to items 246 and 251)

When neither a parser nor an author-level endorse is right, an untrusted value
reaching a dangerous sink is gated on a **typed human approval** from the MCP
operator layer ([operator.py](../../src/revl/mcp/operator.py), item 246 class
(c)). The approval *is* the declassification: it is recorded in the approval
ledger (item 248), it is attributed, and it is distillable into a taint-scoped
policy rule by item 251, whose distillation is already specified as "scoped to
capability x realm x taint-origin (249)". So the third declassifier is not a new
mechanism either; it is the approval layer, with taint origin as one of its keys.

### The refusal, stated precisely

An untrusted value reaching a refusal sink with **none of the three
declassifiers on its data-flow path** is a compile error in v1 (a runtime gate
event in v2 for the policy-gated sinks). The diagnostic names the origin, the
sink, and the shortest declassification the author could add, reusing the
witness-chain machinery `_EmissionEvidence` already builds for G4.

## Decision 4: the refusal sinks

The principle is narrow and load-bearing: **untrusted input cannot DIRECTLY
create authority.** That splits the sinks into two tiers.

### Absolute-refusal sinks (untrusted is refused unless declassified)

These are the sinks where an untrusted value *is* authority. They refuse in v1
at compile time:

| Sink | Why it is authority |
| --- | --- |
| shell command string | a shell string is arbitrary host execution (item 252's terminal tool, `sh -c`) |
| capability / required-key name | a string that selects a boundary is dynamic authority selection |
| policy update | an item-33 policy edit, or an item-251 distilled rule, is authority itself |
| secret sinks (item 256) | logs, ordinary JSON serialization, MCP tool return, an unapproved realm crossing |

The secret row is item 256's information-flow, folded in from the top of the
lattice: `secret:<name>` is the most restricted origin, refused at *every* sink
except the one bound emission it is configured for. This is what the roadmap
means by "with taint (249) the story completes from both sides": secret-origin
taint refused everywhere, and 256's construction (a secret has no read path in
the language) refused at the source. The two together mean an API key cannot
appear in the model context, the transcript, or any other emission.

### Policy-gated sinks (untrusted is allowed but gated, never refused outright)

These are the sinks where untrusted content legitimately flows but the flow is
the dangerous edge of the lethal trifecta (untrusted input plus private data
plus an outbound channel). They are **not** absolute refusals; they are
item-33 policy decisions with a why-trace:

| Sink | Default treatment |
| --- | --- |
| outbound send (`send.*`, network emission) carrying web/net taint | policy-gated: "web-tainted values may not reach `send.*` without ack" (item 33). This is the canonical exfiltration edge, and 249 turns it into a gate event with a why-trace, not a hope about prompt wording. Ack is the approval declassifier (#3). |
| the LLM prompt, carrying web/fs/net taint | **rendered, not refused.** Tool results are *supposed* to reach the model; refusing that breaks the harness. The taint is carried as provenance so the harness can render untrusted spans as untrusted to the model and the operator (roadmap payoff (a)). The prompt refuses only `secret:<name>` origin (256). |

The model-prompt row is the important nuance: a blanket refusal of untrusted
content at the model boundary would be wrong, because a summarizer's whole job is
to read untrusted content and put it in the prompt. The defence is not to refuse
the flow but to **preserve its provenance** into the model context and refuse
only the one origin (secret) that must never reach the model. Web and fs taint at
the prompt is a rendering and audit fact, not a refusal.

## Decision 5: provenance for audit and OTel

Taint is only a defence if a reviewer and an incident responder can see it. Two
surfaces carry it.

### The G8 audit surface (static, v1)

The audit graph already enumerates boundary crossings as stable tokens
([audit_diff.py](../../src/revl/audit_diff.py) `crossings`):
`emit:<component>:<label>` and `host:<component>:<name>`. 249 adds two token
kinds drawn from the same per-component boundary table:

```
taint:<component>:<origin>        a value of <origin> reaches an emission here
declassify:<component>:<origin>   an untrusted value of <origin> is declassified here
```

Both flow through `revl audit --diff` unchanged: a **newly appearing**
`declassify:` or `taint:` token is a widening and fails the drift gate, so a
regeneration that quietly adds an `endorse`, or newly routes web taint into a
send, does not pass silently. This is the same mechanism that already catches "the
agent added one more call," now applied to "the agent added one more
declassification." The item-33 policy engine ([policy.py](../../src/revl/policy.py))
reads the same tokens, so a policy can say `component * may not declassify web`
or `realm billing may not reach send with net-taint` with no new analysis, only
new tokens over the existing audit graph.

Because v1 is static, these tokens are an over-approximation (they name every
origin that *could* reach the crossing, not every origin that *did*). That is
consistent with the rest of the audit surface, which is already an
over-approximation.

### OTel spans (dynamic refinement, v2, ties to item 120)

The OTel export ([otel.py](../../src/revl/otel.py)) already maps an emission to a
span event and carries the causal "why." 249 extends the emission event with the
taint origins of the emission's arguments:

```
event  cause:emission
  revl.taint.origins = ["web:example.com", "model"]
```

and makes a declassification (endorse, or an approval) its own span event
(`declassify:<origin>` with the `reason` and the approver). Combined with the
folded proof-carrying-telemetry note on item 120 (external proposal #15:
`source_hash`, `attestation_hash`, `capability`, `policy`, `lifecycle` on every
span), an incident answers in one place: *did untrusted web content reach this
send, which source ran, under which policy, and where was it endorsed or
approved.* This is the roadmap payoff (b): the exfiltration pattern is a gate
event with a why-trace, not a guess.

The v1 audit tokens are the static over-approximation; the v2 runtime tag makes
the OTel origins the *exact* set a given run carried. The two are consistent by
construction: the runtime origin set is always a subset of the statically
declared one.

## Slice plan

### Slice A: static half (now, no runtime seam) -- ships the guarantee

- `Untrusted[T]` / `Trusted[T]` in [typecheck.py](../../src/revl/typecheck.py),
  as a type qualifier orthogonal to the base type (open question 2), aligned with
  256's `Secret[T]` decision.
- The taint set-union fixed point as a companion to
  [`_emitting_capabilities`](../../src/revl/emission_analysis.py), seeded from
  crossing origin labels, reusing the witness-chain evidence so a refusal prints
  the shortest tainting path (`_EmissionEvidence` already does this for G4).
- The two sink tiers of Decision 4 enforced in the checker / lower
  ([lower.py](../../src/revl/lower.py)): absolute-refusal sinks are compile
  errors; policy-gated sinks emit `taint:`/`declassify:` tokens for the policy
  engine.
- The three declassifiers of Decision 3: the parser is by-construction (no new
  form beyond the `Untrusted`/`Trusted` types); `endorse` is a new capability-
  scoped operator; the approval edge is a hook into
  [operator.py](../../src/revl/mcp/operator.py) (item 246).
- Audit tokens in [audit_diff.py](../../src/revl/audit_diff.py) and policy
  vocabulary in [policy.py](../../src/revl/policy.py).
- A new guarantee code (open question 1). Recommended **G9: "untrusted data
  cannot create authority without a declared declassification."** It is distinct
  from G4 (which bounds *reach*) and G8 (which makes the boundary *enumerable*):
  G9 bounds *flow*. Registered in
  [diagnostics.py](../../src/revl/diagnostics.py) `GUARANTEES`/`FIXES` with its
  fix line, and covered by the existing `test_explain_every_guarantee_has_a_fix`
  totality test.

Additive: no existing program uses the `Untrusted`/`Trusted` qualifiers, the
backends are untouched, all six tiers stay green. Tested at the parse / check /
audit level, mirroring 243 Slice 1.

### Slice B: dynamic runtime taint (behind 243 Slice 2) -- ships the precision

- The per-tier runtime value-representation change: a value carries an origin
  tag alongside it on hosted tiers, propagated through runtime operations,
  exactly as the `Int` bound is a runtime-tracked refinement.
- Runtime propagation of the join through concatenation, record construction,
  and calls; runtime refinement of the static coarse origin to the exact
  `web:<host>` set.
- The exact-origin feed into OTel (Decision 5).
- **Queues behind 243 Slice 2** because it edits the same six-tier runtime seam;
  inherits its per-tier ownership (wasm first-party, python fork, rust/ts/java
  upstream) and its Rust deferral behind item 278. Async-extern-scale like 243.

### Slice C: the two ends (alongside B)

- Item 256: `secret:<name>` as the top of the lattice, refused at every sink but
  its bound emission; the audit gains 256's secrets table alongside the taint
  tokens.
- Items 246 / 251: approval-as-declassification recorded in the ledger and
  distilled into taint-origin-scoped policy (251 already names this seam).

## Residual risks (stated before someone finds them the hard way)

1. **A lying extern classification lies about taint (the G8 caveat).** An extern
   that really reads the network but is declared `pure` mints no origin, so its
   return is believed trusted. Taint origin is declared per crossing, never
   inferred, so this is the same trust-the-declaration boundary as capabilities
   and witnessed status. Same family as prompt-injection-resistance residual #3.
2. **In-policy misuse within one origin.** Taint stops untrusted content from
   *creating* authority; it does not judge the *content* of an in-bounds,
   trusted-origin write. That is prompt-injection-resistance residual #1's
   remaining core, narrowed but not erased.
3. **Over-approximation in v1 can force spurious declassification.** Until the
   runtime tag lands (Slice B), the static labels may mark a value untrusted that
   a specific run would not, pushing an author toward an `endorse` they did not
   truly need. The mitigation is field-granular records and per-module inference
   (Decision 2); the cure is Slice B.
4. **`endorse` is a real downgrade.** It is auditable, diffable, and
   policy-forbiddable, but a granted `endorse` capability is genuine authority to
   launder taint. This is the same shape as granting an over-broad capability
   (prompt-injection-resistance residual #2): the gate enforces the declaration
   faithfully; it cannot know the declaration was too generous.

## Open questions for the user

1. **Guarantee code.** Mint **G9** for information-flow, or fold taint refusals
   under G4? Recommendation: G9. Flow is a distinct guarantee from reach.
2. **Type qualifier vs type constructor.** Is `Untrusted[T]` a qualifier
   orthogonal to `T` (recommended, avoids a combinatorial type explosion and
   matches how capabilities attach), or a genuine type constructor with its own
   eliminators (as 256's `Secret[T]` leans, with *no* eliminators)? These should
   be decided together with 256 so the two information-flow types share a
   mechanism.
3. **Record granularity cost.** Field-granular taint (recommended) requires the
   checker to track taint per field through record types. Confirm the cost is
   acceptable versus whole-record tainting.
4. **The model-prompt sink.** Confirm the intended harness behaviour: render
   web/fs/net taint into the prompt (carrying provenance) and refuse only
   `secret:*`, rather than refusing all untrusted content at the model boundary.
5. **Capability-scoped `endorse`.** Recommendation: yes, so the item-33 policy
   can forbid declassification per component and realm. Confirm the extra surface
   is wanted in v1.
6. **Inference boundary.** Recommendation: infer taint through pure functions
   within a module, require an explicit `Untrusted`/`Trusted` label at published
   service interfaces (so an interface's information-flow contract is visible to
   its consumers). Confirm this is the right line.
