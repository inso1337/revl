# 257: The typed model boundary (completions with a checked shape)

Status: DESIGN (implementation pending). No compiler code changes land with
this note; it designs the slices.

Roadmap: item 257 (`docs/v2.0-roadmap.md:3638`). Reconciles with 44 (idempotent
delivery / auto-retry, `backends/python/runtime.py:76-109`), the MCP schema
machinery (`src/revl/mcp/schema.py`, the reuse core), 249 (taint/provenance:
the completion is an `Untrusted[T]` source), 243/247 and the teardown contract
(G5/G7, `docs/design/teardown-contract.md`), 309 (at-most-once across
abort-then-crash), and 260 (emission cardinality and budgets: the retry
multiplier is a crossing ceiling).

## 1. The one emission whose response executes

Every boundary crossing in revl returns data the body then uses, but one
crossing is different in kind: the model completion is the emission whose
response the whole system then EXECUTES. A filesystem read returns bytes a body
inspects; a completion returns a plan a body DISPATCHES on, and today it returns
`Str`. The agent loop that ships in the test fixtures
(`backends/typescript/tests/fixtures/fr1_loop.rvl:20-33`,
`async_agent_loop.rvl:20-35`) shows the consequence in miniature:

```revl
type ToolReq = { name: Str, args: Str }
type Step = Final(Str) | NeedTool(ToolReq)

fn decode_response(resp: Str) -> Step {
  if (resp.slice(0, 6) == "FINAL ") {
    return Final(resp.slice(6, resp.length()))
  }
  return NeedTool({ name: resp.slice(0, 10), args: "" })
}
```

The response type is `Str`, so the loop cannot dispatch on it until a
hand-written function string-slices it back into a shape. Every such decode is
bespoke, every one is a place a malformed completion becomes a silent wrong
turn (here: any response without the `FINAL ` prefix is read as a tool call
whose name is the first ten characters, whatever they are), and nothing checks
that the model's output was ever a well-formed plan at all. The stringly return
is the root; `decode_response` is the symptom.

This design declares the response type instead, derives the JSON Schema from
that type using the mapping revl already owns, validates the completion against
it at the boundary regardless of what the provider did, and turns a malformed
response into a typed fault rather than a bespoke bug. Downstream the loop stops
parsing and starts matching.

The principle is bigger than the model, and the design keeps it general: ANY
emission may declare a structured response type and earn boundary validation
derived from it. The model is only the emission where it pays most, because the
model is the emission whose response is executed.

## 2. The declaration surface

An emission declares its response type where it already declares a return type
(`MethodDecl.returns` / `ExternDecl.returns`, both `str | None`,
`src/revl/parser.py:39,474`), and opts into boundary validation with a
`validated` modifier in the same slot as `async` / `idempotent` / `deferred`:

```revl sketch
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

service Model {
  emission validated fn complete(ctx: List[Str]) -> AgentTurn
}
```

or, as a first-party extern with a host body:

```revl sketch
extern emission[model] validated async fn complete(ctx: List[Str]) -> AgentTurn
  = @py { ... }
```

`validated` is the whole surface. It is opt-in and defaults off, so every
existing emission (`emission fn complete(...) -> Str` in the fixtures, every
`emission` extern in the corpus) parses and lowers byte-identically; a plain
`Str`-returning emission is unchanged. `validated` is legal only on an
`emission` (a `pure`/`acquire`/`witnessed` classification has no response to
validate at a one-way boundary), and it requires a declared return type (a
`validated` emission returning `Unit` is a `PolicyError` at lower time: there is
nothing to validate). These two rules are checked in `lower.py`, the same place
the other modifier-validity rules live (`lower.py:2518-2579`).

The general rule (section 1) is exactly this modifier. `validated` on
`emission[net] fn fetch_order(id: Str) -> Order` gets the same schema-derivation
and validate-on-response as the model boundary. Nothing about the mechanism is
model-specific; the model extern is the flagship user of a general facility.

## 3. Schema derivation: run the existing mapping at this boundary

### 3.1 The reuse core

revl already maps a surface type to a JSON Schema fragment:
`json_schema_for(type_name: str | None, types: dict | None) -> dict` in
`src/revl/mcp/schema.py:56`. It is what projects a service operation to an MCP
tool's `inputSchema`/`outputSchema` (`schema.py:137,171`). It takes the type as
a NAME STRING, which is exactly how a return type is carried in the IR
(`returns: str | None`), so deriving the boundary schema is one call against the
shape that is already there. There is no second mapping and this design forbids
inventing one: a divergence between "the schema MCP shows" and "the schema the
boundary validates" would be a latent bug factory. One derivation, two consumers
(MCP projection and boundary validation), pinned by a shared test.

The mapping handles primitives (`_JSON_TYPES`, `schema.py:25-32`), `List[T]`,
`Opt[T]` (as `nullable`), `Map[K, V]` (dropping `K`), `Result[T, E]` (as an
untagged `oneOf`), records (structural objects, all fields required), and
enum-shaped variants (all cases payload-free) as a JSON Schema `enum`
(`schema.py:66-87`).

### 3.2 The gap this design must close first: payload-carrying variants

Here is the load-bearing fact, and it is a problem at the center of the feature,
not a corner. The flagship response type `AgentTurn = Final(Str) |
ToolCalls(List[Call])` is a payload-CARRYING variant, and today the mapping
cannot express it. `json_schema_for` renders a variant as an `enum` only when
every case is payload-free; any case with a payload falls through to a stub
(`schema.py:83-87`):

```python
if spec and spec.get("kind") == "variant":
    return {"enum": [case["name"] for case in spec.get("cases") or []]} \
        if all(not c.get("payload") for c in spec.get("cases") or []) \
        else {"x-revlType": type_name}
```

`{"x-revlType": "AgentTurn"}` is an UNCONSTRAINED schema (an object with one
`x-` documentation key and no constraints). Deriving that at the boundary and
validating a response against it is a no-op: every possible response validates.
The guarantee the feature exists to provide would be vacuous on the exact type
the feature exists to check. So the first slice's core work is not the boundary
plumbing; it is teaching the one mapping to render a payload-carrying variant as
a real schema. This improvement is not model-specific and pays a second debt:
MCP's own `outputSchema` for any ADT-returning tool is a stub today, and this
fixes it in the same place.

The rendering is a DISCRIMINATED union, and the discriminator is mandatory (the
reason is the CRITICAL of section 8, so it is decided here). A payload-carrying
variant `T = A(X) | B(Y) | C` derives:

```json
{
  "oneOf": [
    {"type": "object",
     "properties": {"tag": {"const": "A"}, "value": <schema X>},
     "required": ["tag", "value"], "additionalProperties": false},
    {"type": "object",
     "properties": {"tag": {"const": "B"}, "value": <schema Y>},
     "required": ["tag", "value"], "additionalProperties": false},
    {"type": "object",
     "properties": {"tag": {"const": "C"}},
     "required": ["tag"], "additionalProperties": false}
  ]
}
```

Each arm is a closed object keyed by a `tag` pinned with `const` to the case
name; a payload-bearing case carries its payload under `value` with the payload
type's own derived schema (recursively via `json_schema_for`); a nullary case
carries `tag` alone. `additionalProperties: false` on every arm plus the
`const`-pinned tag makes the arms mutually exclusive: a well-formed value
matches exactly one, and the discriminator names which constructor without the
validator having to guess. The tagged wire shape (`{"tag": "...", "value": ...}`)
is the boundary encoding for a validated ADT response; section 4 pins that the
runtime constructs the revl `adt` value (`lower.py:4110-4123`,
`{"kind": "adt", "type", "case", "args"}`) from the tag and value after
validation, so the tagged shape never leaks into the matched-over revl value.

`Result[T, E]` stays its shipped untagged `oneOf` (`schema.py:74`) for MCP
compatibility, but a `validated` emission returning a bare `Result` is refused
(section 3.3) precisely because that untagged form is ambiguous; a caller who
wants a validated two-arm response declares a named tagged variant.

### 3.3 Types the mapping cannot express: refuse to declare, do not silently pass

MCP can afford honest degradation: an `x-revlType` stub in an `outputSchema` is
documentation a human or an agent reads, and an unconstrained schema there
misleads no checker. A VALIDATING boundary cannot afford it: an unconstrained
schema accepts everything, so a `validated` emission whose derived schema still
carries `x-revlType` (anywhere, including nested) provides no guarantee while
claiming one. That is worse than not validating, because it reads as safe.

So the boundary is STRICTER than MCP projection: `lower.py` derives the schema
for a `validated` emission's return type at compile time and refuses the program
if the result contains an `x-revlType` marker at any depth, naming the offending
type (`fault-sweep`-style: "`complete` is `validated` but its response type
`AgentTurn` contains `Foo`, which has no JSON-Schema derivation; a validated
response type must be fully expressible"). After section 3.2 lands, the residual
unexpressible types are: unknown nominal types (a `type` the IR does not carry),
and any type whose derivation is inherently lossy for validation, namely a bare
`Result[T, E]` (untagged `oneOf`, section 3.2) and `Map[K, V]` where `K` is not
`Str` (the mapping drops `K`, so a validator cannot enforce the key type; JSON
object keys are strings, so `Map[Str, V]` is fine and a non-`Str` key map is
refused for a validated return). The refusal is a compile-time property, listed
on the audit surface, never a runtime surprise.

## 4. The provider-extern contract

Two obligations, and the second does not depend on the first.

1. **Pass the schema to constrained decoding where the provider supports it.**
   The derived schema is a compile-time constant (it comes from the return
   type), so the emitter materializes it as a literal the extern body can read,
   alongside the `_revl_config["provider"]` seam a config-extern already uses
   (`backends/python/emit.py:2318-2346,2373-2377`). A provider whose API accepts
   a response schema (structured-output / JSON-schema-constrained decoding) is
   handed it, so the model is steered toward a well-formed response in the first
   place. This is an optimization: it raises the odds the first attempt
   validates and lowers retry cost.

2. **Validate the response against the schema regardless.** The guarantee lives
   on the revl side and never depends on the provider's cooperation. Whether the
   provider constrained its decoding, ignored the schema, silently truncated,
   returned a plausible-but-wrong shape, or does not support schemas at all, the
   runtime validates the returned value against the derived schema before the
   value is handed to the body. The response comes back today as the plain
   return of the emission call (`backends/python/replay.py:779-784`, the
   `_ServiceProxy` emission wrapper returns `attr(*args)` verbatim and records
   only call metadata, never the value). The validate seam wraps exactly that
   return: for a `validated` emission the emitter renders the call as
   `_revl_validate(<call>, <schema-literal>, <where>)` at the emission call site
   (`emit.py:799-821` / `_emit_fire` at `emit.py:1148-1161`), so both the
   service-proxy path and the direct-extern path are covered by one seam.
   `_revl_validate` checks the value against the schema and, on success,
   constructs the revl `adt` value from the validated tag/value (section 3.2);
   on failure it raises the typed fault of section 5.

The division of labor is the honest one: the provider's cooperation is a
speed-up, the revl-side validation is the promise. A design that let the promise
ride on the provider passing the schema would be no promise at all, because the
provider is the untrusted party whose output is the thing under suspicion.

## 5. The typed-fault path: safe-to-retry, budget declared, no double-emit

A response that fails validation is a TYPED fault, not a stringly parse error a
body forgets to handle. It is `retryable`, and the design is precise about WHY,
WHERE the budget lives, and why a retry cannot double-emit.

### 5.1 What makes a completion safe-to-retry

Item 44 gives the vocabulary: an emission is at-most-once by default, and only a
`checked` property earns the runtime a retry right
(`docs/delivery-semantics.md`, `runtime.py:76-83`). Item 44's right is for an
`idempotent` emission whose re-DELIVERY is defined to have the same durable
effect as one delivery (`f(f(x)) == f(x)` on the server). A completion is NOT
idempotent in that sense: re-issuing it produces a different answer and charges
again. Its retry right is a DIFFERENT one, and item 257 names it exactly: a
completion is a read with a cost.

The classification, stated precisely: a completion's only downstream-visible
effect is the value it returns. On a validation failure that value is malformed
and is DISCARDED before the body ever sees it. Nothing downstream consumed it;
no `emit` step fired on it; no witnessed transaction or acquire bracket was
registered from it (the body has not resumed). Re-issuing therefore cannot
double any effect the system executes, because no such effect exists yet at the
moment of the fault. The one thing a retry does re-incur is the provider call
itself: another token charge, another rate-limit unit. That cost is real and is
not undone (an emission is never claimed undone, `src/revl/fault.py:31-33`),
which is exactly why the right is "a read WITH A COST" and why the number of
retries must be bounded rather than hoped.

This is a THIRD mechanism, deliberately distinct from item 44's
idempotent-delivery retry (`retry_idempotent` on `TransientError`,
`runtime.py:86-109`) and from item 243/309 witnessed teardown. A `validated`
emission does NOT ride the `idempotent` path (it is not idempotent, and it must
never be marked so to get retries). Its retry is keyed on ONE fault kind, the
validation fault, and on nothing else: a `TransientError` from a `validated`
emission follows item 44's rule unchanged (retried only if also `idempotent`),
and a non-transient host error is never retried.

### 5.2 Where the budget lives

The retry budget is DECLARED, in the same clause vocabulary as the rest of the
emission surface, not hidden in a wrapper's default:

```revl sketch
extern emission[model] validated retry 2 async fn complete(ctx: List[Str])
  -> AgentTurn = @py { ... }
```

`retry N` on a `validated` emission sets the validation-retry budget (attempts
beyond the first) to a small non-negative integer. Absent the clause the budget
is 0 (a validation fault is terminal, matched by the body, no silent
re-issue); a program that wants re-issue asks for it by number. The clause is
legal only alongside `validated` (there is no validation fault to retry without
it). Budget exhaustion is a TERMINAL typed fault the body observes; it is never
an unbounded loop (section 8, attack 4). The budget rides the IR crossing so the
audit surface and item 260's cardinality accounting can read it: a `validated
retry 2` model emission crossed once per activation counts as up to 3 model
crossings, and item 260's `model.complete: <= 3 per activation` ceiling includes
the retry multiplier by construction.

### 5.3 Why a retry composes with teardown and never double-emits

The seam sits at the emission call site, BEFORE the returned value is bound and
before the body resumes (section 4). So on a validation fault:

- No teardown entry has been registered from the response. The
  acquire/witnessed/compensation stack (`runtime.py:582-639`) is untouched by a
  completion whose value never reached the body, so a re-issue reverts nothing
  and re-registers nothing. G5 (no emission in teardown,
  `lower.py:2128-2177`) and G7 (LIFO revert, `activation.py:22-25,171-180`) are
  not in play, because the fault is at a forward crossing, not in unwind.
- The retry re-crosses the one-way model boundary again, and that crossing is
  the entire re-incurred effect: it is recorded as its own emission in the WAL
  (`replay.py:299-312`), so N attempts are N honestly-recorded crossings, never
  one crossing replayed. There is no partial effect to leak because the response
  value is the only product and the malformed one is dropped whole; validation
  is all-or-nothing on the complete response (section 8, attack 6: nothing
  dispatches on a partial or streamed response).
- Item 309's at-most-once fence is about undeclared inverses across
  abort-then-crash (`runtime.py:629-639`); it is untouched here because a
  completion has no inverse and registers none. The retry story and the teardown
  story meet only at the classification boundary and do not interfere.

## 6. Downstream: match, not parse

The fixture agent loop, before and after. Before (today), the loop cannot
dispatch on the `Str` completion, so a bespoke `decode_response` string-slices
it (`fr1_loop.rvl:20-33`):

```revl sketch
fn decode_response(resp: Str) -> Step {
  if (resp.slice(0, 6) == "FINAL ") { return Final(resp.slice(6, resp.length())) }
  return NeedTool({ name: resp.slice(0, 10), args: "" })
}

fn run_loop(msgs: List[Str], step: (List[Str]) -> Step, n: Int) -> Str {
  return match step(msgs) {
    Final(answer) => answer,
    NeedTool(req) => run_loop(msgs.push(req.name), step, n - 1),
  }
}
// bound at the call site:
//   msgs2 => decode_response(emit model.complete(msgs2))
```

After, the completion IS the `AgentTurn`, validated at the boundary; the loop
matches it directly and `decode_response` is deleted:

```revl sketch
type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

fn run_loop(ctx: List[Str], n: Int) -> Str {
  return match emit model.complete(ctx) {
    Final(answer)    => answer,
    ToolCalls(calls) => run_loop(ctx.push(calls.first().tool), n - 1),
  }
}
```

The `match` is the same construct revl already checks and lowers (arms as
`{"pattern", "bind", "body", "payload_type"}`, `lower.py:5389-5406`;
exhaustiveness and case-name validity at `lower.py:1572-1607`). The gain is not
new matching machinery; it is that the value being matched is now a checked
`AgentTurn` instead of a `Str` a hand-written function guessed at. A malformed
completion no longer produces a `NeedTool` whose name is ten arbitrary
characters; it produces a typed fault before the loop ever matches.

## 7. G-invariant interaction and the honest boundary

The guarantee is SHAPE, not semantics. A validated `AgentTurn` is well-formed
(the tag is one of `Final` / `ToolCalls`, a `ToolCalls` carries a JSON array of
objects each with `tool: Str` and `args: Str`); it is NOT correct, safe, or
trustworthy. This section states the limit plainly, the way the sibling designs
state their caveats, so the feature is not oversold.

- **Taint (item 249) still applies, unchanged.** The completion is one of item
  249's three untrusted-data sources (its own threat model names "an LLM
  completion... item 257's typed model boundary",
  `docs/design/249-taint-provenance.md:26-27`), and the `model` origin class
  mints an `Untrusted` source (`src/revl/taint.py:_SOURCE_CLASS_SCOPES`).
  Validation does not launder that. A validated `ToolCalls(calls)` carries
  `calls` whose `tool` and `args` are model-CHOSEN, hence `Untrusted[Str]`.
  Matching the ADT binds untrusted payloads, and G9 still refuses those payloads
  at a declared sink (a shell string, an authority selection, an instruction
  channel). "It is typed now" is not a declassifier; only `endorse` / a
  `verified fn` is, and this design adds no new laundering path. Shape-checking
  and taint are orthogonal axes, both able to refuse, neither substituting for
  the other.
- **What the boundary actually buys.** It converts the "is this even a
  well-formed plan" class of bugs (unparseable JSON, missing fields, a tool call
  where a final answer was expected, a third variant that does not exist) from a
  silent wrong turn into a typed, boundable, retryable fault at one named place.
  That is a real and large win at the one emission whose response executes. It
  buys nothing about whether the model chose the RIGHT tool or SAFE arguments;
  that is the model's judgment, and confinement (the item-33 boundary policy)
  plus taint are what bound the blast radius of a bad choice, exactly as they do
  today for the `Str` completion.

## 8. Adversarial self-review

Every prior design review in this series found a CRITICAL. Here is this one's,
first.

**CRITICAL - the validating boundary validates against an unconstrained schema,
so the guarantee is vacuous on its own flagship type.** `AgentTurn` is a
payload-carrying variant, and today `json_schema_for` renders it as
`{"x-revlType": "AgentTurn"}` (`schema.py:83-87`), an unconstrained schema that
accepts every JSON value. A first cut that wired "derive the schema, validate
the response" onto the existing mapping would ship a boundary that validates
nothing while advertising a checked shape: every malformed completion passes,
and the feature is worse than absent because it reads as safe. Even after
teaching the mapping to emit a union, an UNTAGGED `oneOf` (the shape `Result` is
rendered as today, `schema.py:74`) reintroduces the same failure one level down:
with `AgentTurn` as `oneOf[Final-arm, ToolCalls-arm]` and no discriminator, a
response shaped like the wrong arm can validate against it, so a malformed
`ToolCalls` validates as a `Final` (or the validator picks an arm by best-effort
and the downstream `match` dispatches to the wrong constructor): a
validated-but-wrong `AgentTurn`, which is the exact silent-wrong-turn the
feature exists to kill, now wearing a checkmark. Mitigation, decided in section
3.2 and made the core of slice 1: the mapping renders payload-carrying variants
as a DISCRIMINATED union (a required `tag` pinned with `const` per case,
`additionalProperties: false` arms), so arms are mutually exclusive and the
constructor is named, not guessed; and `lower.py` REFUSES to compile a
`validated` emission whose derived schema still contains an `x-revlType` marker
at any depth (section 3.3), so an unexpressible response type is a compile-time
error, never a silently-unconstrained runtime pass. The two halves together
(no stub reaches validation, and the union that replaces it is unambiguous) are
what make the guarantee real.

**Attack 2 - the provider ignores the schema and returns malformed; validation
is bypassed.** A provider that does not support constrained decoding, or
supports it and lies, returns a response the schema forbids. Mitigation: the
guarantee never depends on the provider (section 4, obligation 2). The
schema-to-provider hand-off is an optimization; `_revl_validate` on the revl
side runs on the returned value regardless and raises the typed fault on a
violation. The one way to defeat this is to not validate on the revl side, which
this design forbids: passing the schema to the provider is never accepted as a
substitute for validating the response. Status: mitigated.

**Attack 3 - a retry re-fires a downstream side effect (double-emit).** If
validation ran AFTER the body consumed the response and fired an `emit` step or
registered a witnessed effect, a retry would re-run that effect. Mitigation: the
validate seam is at the forward emission crossing, before the value binds and
before the body resumes (section 4, 5.3). At the moment of a validation fault no
teardown entry exists from the response and no downstream `emit` has fired, so a
re-issue doubles nothing the system executes. The only re-incurred effect is the
model crossing itself, which is honestly recorded per attempt and bounded by the
declared budget. Status: mitigated, and it is the reason the classification is
"read with a cost" rather than "idempotent".

**Attack 4 - unbounded retry.** A response that fails validation every time
(a provider stuck returning garbage, an over-strict schema) retried without
bound is an infinite loop that also spends money every iteration. Mitigation:
the budget is a DECLARED small integer (`retry N`, default 0, section 5.2), a
hard ceiling; exhaustion is a terminal typed fault the body must handle, not a
loop. The budget rides the IR crossing and enters item 260's cardinality
ceiling, so an unbounded model spend is a compile-visible property, not a
surprise invoice. Status: mitigated.

**Attack 5 - taint laundering through "now it is typed".** An attacker hopes
that giving the completion a rich type strips the `Untrusted` qualifier, so
`ToolCalls(calls).args` flows to a shell sink as trusted. Mitigation: validation
is orthogonal to taint (section 7); the boundary return carries the `model`
origin exactly as the `Str` completion does today, the constructed `AgentTurn`
and its payloads are `Untrusted`, and G9 still refuses them at sinks. The design
adds NO declassifier. OPEN (flagged, low severity, owned by 249): the
implementation must ensure the post-validation `adt` construction
(`_revl_validate` building `{"kind":"adt",...}`, section 4) does not route
through any path that mints a `Trusted` value; the value must inherit the
crossing's `Untrusted[model]` provenance. This is an implementation obligation
on the taint model's Slice B/E runtime tag, called out so it is not discovered
late; it is not a hole in this design, but it is where a careless
implementation could open one.

**Attack 6 - a partial or streamed response is executed before validation.** A
streaming provider yields tokens incrementally; if the body dispatched on a
prefix, a validated shape could be claimed while the tail is malformed.
Mitigation: `_revl_validate` runs on the COMPLETE response and validation is
all-or-nothing; no arm of the downstream `match` sees a value until the whole
response validated. Streaming is a provider-side transport detail invisible to
the boundary, which sees one finished value. Status: mitigated by construction.

## 9. Sliced implementation plan

**Slice 1 (landable alone): the type-derived schema plus validate-on-response,
no retry.** This slice makes the guarantee real and is useful without any retry.

- `src/revl/mcp/schema.py`: teach `json_schema_for` to render a
  payload-carrying variant as a discriminated union (section 3.2). Pure addition
  to the existing function; enum-shaped variants and every other type are
  byte-identical, so the MCP projection tests stay green except the ADT
  `outputSchema` cases, which gain a real schema (a fix, pinned by an updated
  golden).
- `src/revl/lower.py`: for a `validated` emission, derive the schema at compile
  time and refuse an `x-revlType`-bearing result (section 3.3), naming the
  offending type; check `validated` is emission-only and non-`Unit`-returning
  (section 2); carry the derived schema (a constant dict) onto the emission's IR
  crossing node.
- `src/revl/parser.py`: the `validated` modifier in the emission modifier loop
  (`parser.py:1485-1498` for externs, `:1896-1920` for methods), defaulting
  off; `MethodDecl` / `ExternDecl` gain a `validated: bool = False` field so
  every existing program parses byte-identically.
- `backends/python/emit.py` + `runtime.py`: a `_revl_validate(value, schema,
  where)` runtime helper (validate the value against the schema; on success
  construct the revl `adt`; on failure raise the typed validation fault); the
  emitter wraps a `validated` emission's call site in it (`emit.py:799-821` /
  `_emit_fire`). Materialize the derived schema as a literal for the constrained
  -decoding hand-off (section 4, obligation 1) behind the config-extern seam.
- Exit: a `validated` model emission whose response is malformed raises a typed
  fault (not a stringly wrong turn); a well-formed one validates and matches; a
  `validated` return type carrying an unexpressible type is a compile error; the
  MCP `outputSchema` for an ADT-returning tool renders the tagged union.

**Slice 2: the declared retry budget.** The `retry N` clause
(`parser.py`, the emission clause vocabulary); the read-with-a-cost retry loop
around the validate seam, distinct from `retry_idempotent` and keyed only on the
validation fault (section 5); exhaustion as a terminal fault; the budget on the
IR crossing and in item 260's cardinality ceiling. Depends on slice 1.

**Slice 3: constrained-decoding across providers, and the general emission.** A
provider-capability seam so a schema-supporting provider gets the hint and others
just get validated; the general case exercised beyond the model (a `validated`
`emission[net]` returning a record), pinning that nothing in slices 1-2 is
model-specific.

## 10. Exit tests

1. **The flagship type validates for real.** `emission validated fn complete(...)
   -> AgentTurn`: a response `{"tag":"Final","value":"done"}` validates and
   matches `Final`; `{"tag":"ToolCalls","value":[{"tool":"grep","args":"x"}]}`
   validates and matches `ToolCalls`; the derived schema is a discriminated union
   with `const`-pinned tags, byte-identical to what MCP now projects for the same
   type.
2. **Malformed is a typed fault, not a parse.** A response missing `tag`, or
   carrying `tag: "Nope"`, or a `ToolCalls` value that is not an array of the
   right objects, raises the typed validation fault; nothing downstream matches;
   the fault names the schema violation.
3. **The ambiguity attack is closed.** With `AgentTurn` rendered as a tagged
   union, a `Final`-shaped payload cannot validate as `ToolCalls` and vice
   versa; a response ambiguous under an untagged `oneOf` is rejected under the
   tagged one. (Regression pinning the CRITICAL.)
4. **Unexpressible response type is a compile error.** `emission validated fn f()
   -> Foo` where `Foo` has no derivation, and `-> Result[Int, Str]` (untagged),
   and `-> Map[Int, Str]` (non-`Str` key), are each refused at lower time with
   the offending type named; the same types on a NON-`validated` emission compile
   unchanged.
5. **The provider is not trusted.** A stub provider that returns a schema-
   violating response despite being handed the schema is caught by revl-side
   validation; a provider that returns a valid response without ever reading the
   schema is accepted. The verdict is identical in both, proving the guarantee is
   revl-side.
6. **Retry is bounded and does not double-emit** (slice 2). A provider returning
   malformed twice then valid, under `retry 2`, yields the valid `AgentTurn` and
   records exactly three model crossings in the WAL; under `retry 0` the first
   malformed response is a terminal fault. A `validated` emission followed by a
   downstream `emit` step, retried, fires the downstream emission exactly once
   (no double-emit), because the retry precedes the body's resumption.
7. **Budget exhaustion is terminal.** A provider that never validates, under
   `retry 1`, raises the terminal fault after two crossings; no unbounded loop;
   item 260 reports the model ceiling including the retry multiplier.
8. **Taint is not laundered.** A validated `ToolCalls(calls)` whose `calls.args`
   flows to a shell sink is refused by G9 exactly as an `Untrusted[Str]` derived
   from a `Str` completion is; the validated ADT and its payloads carry the
   `model` origin.
9. **Byte-identical when absent.** A program with no `validated` emission parses,
   lowers, and emits byte-identically to today (existing corpus green,
   `emission fn ... -> Str` unchanged, MCP projection unchanged except the ADT
   `outputSchema` fix).
10. **The general rule holds** (slice 3). A `validated emission[net] fn
    fetch(...) -> Order` (a record return) derives a structural object schema and
    validates the response with the same seam, no model involved.

## 11. Non-goals and open questions

Non-goals:

- No semantic validation of model choices (section 7): shape only, never "is
  this the right or safe tool".
- No new declassifier and no taint change: the validated response is
  `Untrusted`, full stop.
- No second type-to-schema mapping: the boundary runs `json_schema_for` or it is
  a bug (section 3.1).
- No automatic validation of every emission return: `validated` is opt-in so the
  IR stays byte-identical and the guarantee is a reviewable declaration.
- No model-specific machinery: the model is the flagship user of a general
  facility.

Open questions:

- **The tagged wire encoding is a choice, not a law.** This design pins
  `{"tag": name, "value": payload}`. A provider ecosystem may standardize a
  different envelope (a bare `{name: payload}` single-key object, or
  OpenAI-style tool-call arrays). The derivation and the post-validation `adt`
  construction are the one place that shape lives, so a later item can add an
  alternate encoding behind the same seam without touching the type or the
  match; slice 1 commits to the explicit-tag form because it is the
  unambiguous one.
- **Multi-field variant payloads.** A case with several payload values
  (`Call(tool, args)` rather than `Call({tool, args})`) is not expressed by the
  current IR (`VariantCase.payload` is a single type string,
  `parser.py:380-384`); this design assumes the record-payload spelling the
  fixtures already use (`ToolReq` as a record). If multi-field constructor
  payloads land later, the union `value` becomes a positional array and the
  derivation follows; noted so it is a known extension, not a surprise.
- **Whether `nullable` in `Opt[T]` derivation** (`schema.py:70`, an OpenAPI-ism
  rather than strict JSON Schema) needs a stricter `{"oneOf": [T, {"type":
  "null"}]}` form for a validating boundary. MCP tolerates `nullable`; a strict
  validator may not. Slice 1 will settle this against the chosen validator and,
  if it must change, change it in the one mapping (which MCP then also gets),
  never forking a second rendering.
