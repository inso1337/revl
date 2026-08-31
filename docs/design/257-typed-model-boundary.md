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

## 0. Revision (adversarial review 2026-08-31)

An independent adversarial review of the first draft found one NEW CRITICAL
beyond the author's self-review (section 8), plus two HIGHs and two MEDIUMs.
This revision folds all five in and re-slices so Slice 1 still lands alone. The
corrected soundness backstop is stated once here and governs the rest of the
doc.

**Corrected soundness backstop (replaces the "sentinel scan"):** the first draft
made the boundary safe by deriving the schema and then scanning the derived dict
for an `x-revlType` sentinel, refusing if one appeared at any depth. That scan is
NECESSARY but NOT SUFFICIENT: three marker-free degradations produce a derived
dict with no `x-revlType` anywhere yet reach the boundary unsound, at ANY depth
(inside a `List`, a record field, a `Map` value, a nested variant payload):

- **Untagged `Result[T, E]`** (`schema.py:74`) renders `{"oneOf": [T, E]}` with
  no discriminator. `Result[Str, Str]` derives two IDENTICAL arms, so a string
  matches both, `oneOf`'s exactly-one rule fails, and every valid response is
  REJECTED. `Result[Int, Str]` derives disjoint arms that validate, but with no
  tag `_revl_validate` cannot tell `Ok` from `Err`, so it dispatches the wrong
  constructor. Neither case carries an `x-revlType` marker; the sentinel scan
  passes them.
- **`Map[K, V]` with `K != Str`** (`schema.py:72`) renders
  `{"type": "object", "additionalProperties": V}`, dropping `K`. The derived
  dict is marker-free and accepts arbitrary string keys, with the key-type
  constraint simply absent.
- **Recursive / cyclic ADTs** (`Json = Null | Arr(List[Json]) | Obj(Map[Str,
  Json])`, or an `AgentTurn` that transitively contains an `AgentTurn`). Once
  section 3.2 makes the renderer recurse into payloads, there is NO base case:
  `json_schema_for` carries no cycle guard (contrast `resolve` in
  `schema.py:256`, which threads a `seen` frozenset). The renderer recurses
  forever and stack-overflows AT COMPILE TIME. That is a compiler crash, not a
  typed refusal. A naive depth cap that bottoms out to a marker-free `{}`
  re-opens wrong-dispatch; one that bottoms out to `x-revlType` makes the entire
  recursive-ADT class un-validatable.

The fix (section 3.3) replaces the derived-dict sentinel scan with a POSITIVE,
TOTAL, CYCLE-AWARE predicate over the SURFACE TYPE:

```revl sketch
fully_expressible(type_name, types, seen) -> bool
```

It recurses the surface type structure (`List` / `Opt` / `Map` / `Result` /
record fields / variant payloads), threads a `seen` set, and returns
non-expressible for: an unknown nominal type, an untagged `Result[T, E]`, a
`Map[K, V]` with `K != Str`, and ANY type that lies on a cycle (unless the
derivation is upgraded to `$ref` / `$defs` and the chosen validator resolves
`$ref`; section 3.3 states what that route would require). This predicate is the
gate for both the `validated`-emission case and the general non-model `validated`
case. The derived-dict `x-revlType` scan is KEPT, but only as a
defense-in-depth assertion after the predicate has already accepted the type,
never as the primary gate.

**Summary of the five findings and how each is closed:**

- **CRITICAL (new): the sentinel scan is necessary but not sufficient.** Closed
  by the cycle-aware `fully_expressible` predicate over the surface type
  (sections 0, 3.3; exit test 4). The three marker-free degradations
  (untagged `Result`, non-`Str`-key `Map`, cyclic ADT) are now refused positively,
  and the compile-time infinite-recursion crash is prevented by the `seen` set.
- **HIGH-1: "<= 3 crossings by construction" is false against shipped 260.**
  260 slice 1 reports every loop and recursion as `unbounded` and has no
  bounded-iteration certification; a retry loop inside the validate seam is ONE
  static crossing, so 260 would report `<= 1` while the true ceiling is `N + 1`.
  Closed (sections 5.2, 8 attack 4) by modeling `retry N` as a STATIC MULTIPLIER
  attribute on the single crossing node and specifying the 260 count-fold change
  that multiplies that crossing's contribution by `N + 1`. The exact `<= N + 1`
  ceiling is stated as an explicit cross-item DEPENDENCY on that fold landing in
  260; the "by construction" claim is DROPPED until it does.
- **HIGH-2: `validated` + `Untrusted[...]` collide at derivation time.** In the
  default profile the `model` source mints only for `Untrusted[T]` returns
  (`taint.py:246`), so the SECURE spelling is `-> Untrusted[AgentTurn]
  validated`; but `json_schema_for` on the un-stripped `Untrusted[AgentTurn]`
  head falls through to `{"x-revlType": ...}` and the refusal would then reject
  the secure program. Closed (section 3) by pinning that schema derivation for a
  `validated` emission runs on the QUALIFIER-STRIPPED return type (after
  `extract_and_normalize`, `taint.py:203-217`); exit test 4a pins that
  `-> Untrusted[AgentTurn] validated` derives the SAME union as `-> AgentTurn`.
- **MEDIUM-1: all-nullary variants keep the `enum` rendering.** `Step = Ready |
  Done` renders wire `"Ready"`, but adding one payload case flips the whole wire
  contract to `{"tag": "Ready"}` and forces two `_revl_validate` construction
  paths. Closed (section 3.2) by rendering ALL VALIDATED variants as the tagged
  union: one uniform wire shape, one construction path. Enum-shaped variants may
  still render as `enum` for plain (non-`validated`) MCP projection; the
  VALIDATED boundary is uniform.
- **MEDIUM-2: "byte-identical when unused" is overstated for MCP projection.**
  The `schema.py` change fires for MCP projection UNCONDITIONALLY (it is not
  gated on `validated`), so every existing service op returning a
  payload-carrying ADT changes its `outputSchema` from `{"x-revlType": ...}` to
  the tagged union with nobody using the feature. Closed (section 11, exit test
  9) by scoping the byte-identical claim to the IR / emit path only, stating the
  MCP-projection goldens delta explicitly, and flagging the blast radius.

**Re-slice.** Slice 1 still lands alone and is: tagged-union rendering for
fully-expressible types, the `fully_expressible` refusal, and
validate-on-response. Retry is deferred to Slice 2; the 260 `N + 1` multiplier is
noted as a cross-item dependency owed to Slice 2, not Slice 1.

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

```revl
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

**Derive on the qualifier-stripped return type (HIGH-2).** The SECURE spelling of
a validated model emission in the default taint profile is
`-> Untrusted[AgentTurn] validated`, not `-> AgentTurn validated`: the `model`
source mints an `Untrusted` origin only for an `Untrusted[T]` return (or under
`--taint-strict`), `taint.py:246,256-263`. But `json_schema_for` on the raw
`Untrusted[AgentTurn]` head does not know that qualifier and falls through to
`{"x-revlType": "Untrusted[AgentTurn]"}` (`schema.py:87`), which the section 3.3
refusal would then reject, refusing the very spelling a secure program must use.
So the derivation for a `validated` emission runs on the QUALIFIER-STRIPPED
return type. `extract_and_normalize` already strips `Untrusted[...]` /
`Trusted[...]` in place before the base checker and every emitter see the type
(`taint.py:203-217,239-241`), so the schema derivation reads the same bare
`AgentTurn` those consumers do. Consequence, pinned by exit test 4a:
`-> Untrusted[AgentTurn] validated` and `-> AgentTurn validated` derive the SAME
union. Qualifier stripping and shape derivation stay orthogonal, exactly as
taint (section 7) and shape are orthogonal at the boundary.

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
reason is the CRITICAL of section 8, so it is decided here). At the VALIDATED
boundary the tagged union is UNIFORM across every variant, payload-carrying or
not (MEDIUM-1). The shipped mapping renders an all-nullary variant as a bare
`enum` (`schema.py:84-86`), so `Step = Ready | Done` ships wire `"Ready"` while
`Step = Ready | Busy(Int)` ships `{"tag": "Ready"}`: adding one payload case
silently flips the entire wire contract and forces `_revl_validate` to carry two
construction paths (a bare-string path and a tagged-object path). The validated
boundary refuses that hazard by rendering ALL its variants tagged: one wire
shape, one construction path, and adding a payload case never changes the
encoding of the cases beside it. The `enum` rendering STAYS for a plain,
non-`validated` MCP projection (an `outputSchema` a human or agent reads, where
the compactness is worth more than uniformity); the split is only between "plain
MCP projection" (enum-shaped may stay `enum`) and "validated boundary" (always
tagged). A payload-carrying variant `T = A(X) | B(Y) | C` derives:

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

### 3.3 The expressibility gate: a positive, total, cycle-aware predicate on the surface type

MCP can afford honest degradation: an `x-revlType` stub in an `outputSchema` is
documentation a human or an agent reads, and an unconstrained schema there
misleads no checker. A VALIDATING boundary cannot afford it: an unconstrained (or
ambiguous) schema fails to constrain, so a `validated` emission whose response
type does not derive an exact schema provides no guarantee while claiming one.
That is worse than not validating, because it reads as safe. So the boundary is
STRICTER than MCP projection and refuses at compile time any `validated` emission
whose response type is not fully expressible.

The first draft implemented that refusal by DERIVING the schema and then SCANNING
the derived dict for an `x-revlType` sentinel at any depth. That scan is
necessary but NOT sufficient, and the reasons are the section 0 / section 8
CRITICAL: three degradations produce a marker-free derived dict yet are unsound
(untagged `Result[T, E]`, `Map[K, V]` with `K != Str`), and a recursive ADT makes
the derivation itself NON-TERMINATING once section 3.2 recurses into payloads
(`json_schema_for` has no cycle guard), which is a compile-time crash, not a
refusal. Deriving-then-scanning cannot be the gate, because for a cyclic type
the derivation never returns a dict to scan.

The gate is instead a POSITIVE, TOTAL, CYCLE-AWARE predicate over the SURFACE
TYPE, evaluated BEFORE any schema is derived:

```revl sketch
fully_expressible(type_name, types, seen) -> bool
```

It structurally recurses the surface type and threads a `seen` set of nominal
type names currently on the derivation path. It returns `false` (non-expressible,
refuse) for:

- an **unknown nominal** type (a name `types` does not carry);
- an **untagged `Result[T, E]`** (its shipped derivation is an untagged `oneOf`,
  `schema.py:74`, which cannot name a constructor and, for `T == E`, cannot even
  admit a valid value; a caller who wants a validated two-arm response declares a
  named tagged variant instead);
- a **`Map[K, V]` with `K != Str`** (the mapping drops `K`, `schema.py:72`, so a
  validator cannot enforce the key type; JSON object keys are strings, so
  `Map[Str, V]` is expressible and any non-`Str`-key map is refused);
- **any type on a cycle**: if `type_name in seen`, the type is (mutually)
  recursive. UNLESS the derivation is upgraded to `$ref` / `$defs` (see below),
  a cyclic type is refused, both because a finite inline schema cannot express it
  and because deriving it would not terminate.

Otherwise it recurses into structure with `type_name` added to `seen`: `List[T]`
and `Opt[T]` recurse on `T`; `Map[Str, V]` recurses on `V`; a record recurses on
every field type; a variant recurses on every case payload type (a nullary case
contributes nothing to recurse). A type is expressible only if every position it
reaches is. Because `seen` grows on every nominal descent and the set of nominal
names is finite, the predicate is TOTAL: it terminates on every input, cyclic or
not, where the naive derivation would not. `fully_expressible` governs BOTH the
`validated` model case and the general non-model `validated` case (section 2's
general rule); it is the one gate.

On refusal, `lower.py` reports `fault-sweep`-style, naming the offending type and
WHY: "`complete` is `validated` but its response type `AgentTurn` reaches `Foo`,
which has no JSON-Schema derivation" / "reaches an untagged `Result[Int, Str]`,
which cannot name its constructor" / "reaches `Map[Int, Str]`, whose key type is
not expressible" / "is recursive (`Json` reaches `Json`), which an inline schema
cannot express; use ... ". The refusal is a compile-time property on the audit
surface, never a runtime surprise.

**The derived-dict `x-revlType` scan is kept as defense in depth, not as the
gate.** After `fully_expressible` accepts a type, `lower.py` still derives the
schema and asserts no `x-revlType` marker survives at any depth. If that
assertion ever fires, it means the predicate and the renderer have drifted apart
(a renderer bug), and the assertion catches it loudly rather than shipping an
unconstrained boundary. It runs only on types the predicate already proved
expressible, so it never meets a cyclic type and never fails to terminate.

**The `$ref` / `$defs` escape hatch for recursive ADTs (deferred, specified).**
A recursive ADT such as `Json = Null | Arr(List[Json]) | Obj(Map[Str, Json])` IS
expressible in JSON Schema, but only with named definitions and internal
references: each recursive nominal type becomes a `$defs` entry and every
back-edge becomes a `{"$ref": "#/$defs/Json"}`. Slice 1 does NOT take this route:
it refuses cyclic types, keeping the derivation inline and finite. A later slice
may lift the cyclic-type refusal by (1) rendering each nominal type on a cycle as
a `$defs` entry and each cyclic reference as a `$ref`, AND (2) pinning that the
CHOSEN validator resolves `$ref` (the same validator-capability question section
11 raises for `nullable`); `fully_expressible` would then treat a `seen` hit as
expressible-via-`$ref` rather than a refusal. Neither half is free, so the
escape hatch is named and deferred, not assumed. A naive depth cap is explicitly
rejected: bottoming a cap out to a marker-free `{}` re-opens wrong-dispatch, and
bottoming it out to `x-revlType` makes the whole recursive-ADT class
un-validatable. The cycle-aware refusal (or, later, `$ref`) is the only sound
choice.

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
an unbounded loop (section 8, attack 4).

**The budget is a STATIC MULTIPLIER on the single crossing node, and the exact
ceiling DEPENDS on a 260 fold change that has not landed (HIGH-1).** The first
draft claimed a `validated retry 2` emission counts as "up to 3 model crossings
by construction" and that 260's `model.complete: <= 3` ceiling includes the
multiplier by construction. That claim is FALSE against shipped 260. 260 slice 1
reports every loop and every recursion as `unbounded` (`cardinality.py:97-127`,
`_classify` / `_reason`: "Slice 1 reports all recursion as unbounded", "the
iteration count is not statically bounded in Slice 1") and has no
bounded-iteration certification yet. A runtime retry loop implemented as an
actual loop around the validate seam would therefore be reported `unbounded`, not
`<= 3`. Modeled instead as a plain crossing, it is ONE static crossing, which the
count-fold (`cardinality.py:138+`, one `+1` per `_boundary` walk arm) counts as
`<= 1`, a false-LOW attested ceiling: exactly the false-LOW defect class the 260
review already caught.

The correction: `retry N` is NOT a loop and NOT a recursion. It is a STATIC
MULTIPLIER attribute carried on the single emission crossing node in the IR. To
get an exact `<= N + 1` ceiling, item 260's count-fold must learn to multiply a
`validated retry N` crossing's contribution by `N + 1` (the first attempt plus N
retries) rather than adding 1. Modeled as a multiplier on a single crossing, this
needs NO bounded-iteration certification: there is no loop whose trip count must
be bounded, only a constant factor on one countable crossing. State the
dependency explicitly:

- An exact `<= N + 1` model-crossing ceiling REQUIRES the 260 count-fold change
  (multiply-by-`N + 1` on a `validated retry N` crossing) to land in 260. It is
  NOT implied by 260 slice 1, and it does NOT wait on bounded-iteration
  certification once modeled as a multiplier.
- Until that fold lands, the "by construction" ceiling claim is DROPPED. The
  budget still rides the IR crossing (so the audit surface can read it and Slice
  2's retry loop is hard-bounded by the declared integer), but item 257 does not
  assert an exact 260 ceiling until the multiplier fold is in. This is a
  cross-item dependency owed to Slice 2, recorded here and in section 9.

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
  `calls` whose `tool` and `args` are model-CHOSEN, and they carry the SAME
  origin the `Str` completion carries today, no more and no less: an `Untrusted`
  origin exactly when the return is spelled `Untrusted[...]` or the compile runs
  `--taint-strict` (`taint.py:246,256-263`), which is the secure default spelling
  section 3.1 pins for the model boundary. Matching the ADT binds those payloads,
  and G9 still refuses them
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
`validated` emission whose response type is not fully expressible (section 3.3),
so an unexpressible response type is a compile-time error, never a
silently-unconstrained runtime pass. The two halves together (no stub reaches
validation, and the union that replaces it is unambiguous) are what make the
guarantee real.

**Adversarial review update (2026-08-31): the refusal was under-specified.** The
first draft implemented the refusal as "derive the schema, then scan the derived
dict for an `x-revlType` sentinel at any depth". The independent review found that
scan NECESSARY but NOT SUFFICIENT (a NEW CRITICAL, section 0): an untagged
`Result[T, E]`, a `Map[K, V]` with `K != Str`, and a recursive/cyclic ADT each
reach the boundary unsound with NO `x-revlType` marker anywhere, and the cyclic
case makes the derivation itself non-terminating (a compile-time crash) so there
is no dict to scan. The corrected backstop (section 3.3) is a positive, total,
cycle-aware `fully_expressible` predicate over the SURFACE type; the sentinel scan
survives only as a defense-in-depth assertion on types the predicate already
accepted.

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
loop. The budget rides the IR crossing as a STATIC MULTIPLIER (section 5.2), so
Slice 2's retry is hard-bounded by the declared integer regardless of 260. The
EXACT `<= N + 1` model-crossing ceiling in item 260 additionally requires the 260
count-fold to multiply the crossing by `N + 1` (a cross-item dependency stated in
section 5.2; NOT true against shipped 260 slice 1, which would report an
actual-loop retry as `unbounded` or a single crossing as a false-LOW `<= 1`).
Status: the unbounded-spend attack is mitigated by the declared integer; the
exact compile-visible ceiling is mitigated only once the 260 multiplier fold
lands, and the "by construction" claim is dropped until then.

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

- `src/revl/mcp/schema.py`: teach `json_schema_for` to render a variant as a
  discriminated union (section 3.2), with a mode flag so the VALIDATED boundary
  renders ALL variants tagged (including all-nullary, MEDIUM-1) while plain MCP
  projection keeps the `enum` rendering for enum-shaped variants. Add the
  `fully_expressible(type_name, types, seen)` predicate (section 3.3) beside the
  renderer. The renderer change is additive for the payload-carrying and
  validated-tagged paths; plain (non-`validated`) MCP projection of enum-shaped
  and primitive/list/record types stays byte-identical, and the ONE deliberate
  MCP-projection delta is the ADT `outputSchema` cases, which gain a real tagged
  schema in place of the `x-revlType` stub (a fix, pinned by an updated golden;
  blast radius enumerated in section 11 / exit test 9).
- `src/revl/lower.py`: for a `validated` emission, run `fully_expressible` on the
  QUALIFIER-STRIPPED return type (section 3.1, HIGH-2) BEFORE deriving, refusing
  and naming the offending type when it returns false (section 3.3); then derive
  the schema and keep the `x-revlType` scan only as a defense-in-depth assertion
  on the already-accepted type. Check `validated` is emission-only and
  non-`Unit`-returning (section 2); carry the derived schema (a constant dict)
  onto the emission's IR crossing node.
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
IR crossing as a STATIC MULTIPLIER (section 5.2). Depends on slice 1. Carries a
CROSS-ITEM DEPENDENCY on item 260 (HIGH-1): an exact `<= N + 1` model-crossing
ceiling requires 260's count-fold to multiply a `validated retry N` crossing by
`N + 1` (`cardinality.py:138+`), which is not implied by 260 slice 1 and is not a
loop/recursion needing bounded-iteration certification. Until that 260 fold
lands, Slice 2 asserts only the declared-integer hard bound, not an exact 260
ceiling.

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
4. **Unexpressible response type is a compile error (the corrected predicate).**
   `fully_expressible` (section 3.3) refuses each of, at lower time, naming the
   offending type: an unknown nominal `-> Foo`; an untagged `-> Result[Int, Str]`
   AND `-> Result[Str, Str]` (the identical-arms case that would reject every
   valid value); a non-`Str`-key `-> Map[Int, Str]`; and each of these NESTED at
   depth (`-> List[Result[Int, Str]]`, a record field of type `Map[Int, Str]`, a
   variant payload `Obj(Map[Int, Json])`) to prove the predicate is not
   top-level-only. The same types on a NON-`validated` emission compile
   unchanged.
   4a. **Cyclic ADTs refuse WITHOUT crashing the compiler.** `Json = Null |
   Arr(List[Json]) | Obj(Map[Str, Json])` and a mutually-recursive pair, as a
   `validated` return, are refused with a "recursive, an inline schema cannot
   express it" message and the compile TERMINATES (the `seen` set prevents the
   non-terminating derivation that a derive-then-scan gate would hit). Pins that
   the gate is the predicate, not the derived-dict scan.
   4b. **Qualifier-stripped derivation (HIGH-2).** `-> Untrusted[AgentTurn]
   validated` derives the SAME discriminated union as `-> AgentTurn validated`
   (derivation runs on the stripped type, section 3.1), so the secure default
   spelling is accepted, not refused; and the taint origin on the result is
   `Untrusted[model]` (exit test 8).
   4c. **All-nullary validated variant renders tagged (MEDIUM-1).** `Step = Ready
   | Done` under a `validated` return derives the tagged union (`{"tag":
   "Ready"}` / `{"tag": "Done"}`), NOT a bare `enum`; adding a payload case
   (`Step = Ready | Busy(Int)`) does not change the encoding of `Ready`. The same
   `Step` in a plain (non-`validated`) MCP `outputSchema` still renders `enum`.
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
   `retry 1`, raises the terminal fault after two crossings; no unbounded loop.
   The exact-`<= N + 1`-ceiling assertion in item 260 is gated on the 260
   count-fold change (section 5.2, HIGH-1): this test asserts the hard
   declared-integer bound now, and the 260 ceiling equality only once the
   multiply-by-`N + 1` fold lands in `cardinality.py`. It does NOT assert
   `<= N + 1` against shipped 260 slice 1 (which would report an actual-loop
   retry as `unbounded` or a single crossing as a false-LOW `<= 1`).
8. **Taint is not laundered.** A validated `ToolCalls(calls)` whose `calls.args`
   flows to a shell sink is refused by G9 exactly as an `Untrusted[Str]` derived
   from a `Str` completion is; the validated ADT and its payloads carry the
   `model` origin.
9. **Byte-identical when absent, scoped to IR / emit (MEDIUM-2).** A program with
   no `validated` emission parses, lowers, and EMITS byte-identically to today
   (existing corpus green, `emission fn ... -> Str` unchanged). The byte-identical
   claim is SCOPED to the IR / emit path; it does NOT extend to MCP projection.
   The `schema.py` change fires for MCP projection UNCONDITIONALLY (it is not
   gated on `validated`, `schema.py:171` calls `json_schema_for(returns, types)`
   for every provided op), so every service op whose return type is a
   payload-carrying variant changes its `outputSchema` from `{"x-revlType": ...}`
   to the tagged union with nobody using the feature. This test therefore ALSO
   asserts the expected MCP-projection GOLDENS DELTA: enumerate the affected
   corpus tools by grepping service ops (provided-method returns) whose type is a
   payload-carrying variant, update their `outputSchema` goldens to the tagged
   union, and confirm the `tests/test_interchange.py` + audit-golden
   (`registry/components/*/evidence/*.json`,
   `registry/components/*/manifest.json`) blast radius: any audit or interchange
   document that snapshots such an `outputSchema` moves in lockstep and its golden
   is regenerated in the same commit. A service op returning a primitive, list,
   record, or enum-shaped variant sees NO MCP delta.
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
  IR / emit path stays byte-identical and the guarantee is a reviewable
  declaration. NOTE the scope (MEDIUM-2): "byte-identical" covers IR and emit, not
  MCP projection. The `schema.py` renderer change is unconditional, so an existing
  service op returning a payload-carrying ADT gains a tagged `outputSchema` even
  with no `validated` anywhere; that MCP goldens delta is intended and enumerated
  (exit test 9), not a regression.
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
