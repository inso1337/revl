# 542: Compiling the typed model boundary into a decoding grammar

Roadmap: item 513 (issue #1187), from the 2026-09-19 external review. Slices 1,
2 and 4 are LANDED. Slice 3 is designed here and not written, and section 6 says
what it costs. Sections 0 to 8 are slice 1's note, revised only where slice 2
measured something it had listed as unverified; sections 9 to 11 are new.

Reconciles with: item 257 and `docs/design/257-typed-model-boundary.md` (the
typed boundary, whose schema this compiles a second time), item 260 (the
emission budget this spends less of), item 512 and
`docs/design/531-model-placement.md` (the placement, §9 of which says this item
inherits it and needs nothing from it), item 515 (the portfolio, which owns the
device profile and therefore owns the question of whether a member supports
constrained decoding at all), `docs/guarantees.md` and `docs/rejections.md`
(the code registry, which this note adds nothing to).

---

## 0. The decision in one paragraph

Item 257 already knows the type a model call must return, and already derives
an exact JSON Schema from it. It uses that schema to *check* the answer. The
model is still asked in prose, answers freely, and the boundary then decides
whether the answer was usable, so a schema-valid response is probabilistic
where it could be mechanical. This note derives, from the same declared type, a
**decoding grammar**: a portable description of the language of the responses
the type accepts. revl states the grammar; a provider performs the constraint.
The compiler gains no ability to make a decoder behave, and claims none. What
it gains is that the constraint is derived rather than hand-written, travels
with the crossing to all six tiers as data, and has a compile-time refusal
where it cannot be derived, so a caller is never told a decode is constrained
when it is not.

## 1. Why this matters most to a small local model

The motivating case is not a frontier model. A large model asked in prose for a
`ToolCalls` object usually produces one, and the retries it costs are a tax. A
7B-class local model asked the same question produces JSON with a trailing
comma, a chatty preamble, an invented field, or a tag that is nearly the right
tag, often enough that the boundary becomes the dominant source of failure.
Constraining the decode does not make that model smarter; it makes the shape
stop being something the model has to get right at all. It converts a
competence problem into a mechanical one, and that conversion is worth far more
where competence is scarce.

This is also the case where the conversion is *cheapest to obtain*: a local
runtime is the one a user can configure, and GBNF is the constrained-decoding
dialect those runtimes already implement. A cloud provider's structured-output
mode is a different surface with a different dialect, and section 6 says how it
is added without reinterpreting these bytes.

The secondary gain is item 260's. A validated crossing with `retry N` counts
`N + 1` against the emission ceiling by construction. Every sample the decoder
cannot produce is a retry not spent, so the ceiling that item 257 made exact
becomes one a program is less likely to reach.

## 2. What revl emits

The whole compiler-side surface is four compile-time constants, bound at the
crossing in the IR beside item 257's `response_schema`:

```
"response_grammar": {
  "format": "gbnf",
  "root":   "root",
  "text":   "root ::= t7\nt1 ::= \"\\\"Final\\\"\"\n...",
  "digest": "a7d58a05..."
}
```

`format` names the dialect. `root` names the start symbol. `text` is the
grammar. `digest` is a SHA-256 over `text`.

Nothing here is a call, a handle, a host reference or a capability. It is text
and a hash, so the same four values reach the python, typescript, go, rust,
java and wasm tiers unchanged, and no tier links a decoder to carry them. A
backend that does not yet pass the grammar anywhere still compiles and still
runs: the field is inert until a provider seam reads it.

`digest` is an identity, not a name. Two crossings that derive the same grammar
from different types share it, and a provider may cache a compiled grammar
under it without revl having to say anything about how a grammar is compiled.
It is the mirror image of item 515's `placement_digest`, which revl binds over
seven fields the *provider* owns and never interprets. Here the compiler owns
the bytes and the provider never rewrites them. Both halves of the cluster obey
the same rule from opposite sides: the compiler states, the provider performs.

## 3. What the provider does with it

It constrains the decode, or it does not. revl does not model that, call it, or
verify it.

> Slice 2 refined the last clause and section 9.2 is the refinement. revl still
> does not model or call a decode. It does now verify one thing, and only one:
> a provider that *claims* to have honoured the stated grammar is held to the
> claim. A provider that claims nothing is still not verified and still not
> refused, for the reason the rest of this section gives.

This is the boundary the item turns on, and getting it wrong in either
direction breaks the feature. Pushing further, so that revl owns a decoder
integration, makes the feature untestable and unportable: a decoder is a host
thing, it differs per runtime, and six tiers would need six of them. Pulling
back, so that revl merely *recommends* a shape in the prompt, makes it
unenforceable, which is the status quo the item exists to replace. A grammar
the compiler derives from a declared type is the largest artifact that is
portable, and it is exactly the artifact a constrained decoder consumes.

Three consequences follow, and all three are load-bearing.

**Item 257's validator stays on, unchanged.** It runs revl-side on every
response regardless of what the provider did. A provider that honours the
grammar makes it succeed every time; a provider that ignores it produces the
same named validation fault as before. This is what keeps an unhonoured grammar
from being a silent downgrade: the caller's guarantee was never "the decoder
was constrained", it was "a response that is not of this shape does not reach
the body", and that guarantee is unchanged.

**The grammar is rendered from the schema, not from the type.** A second walk
over the surface type would be a second derivation that could drift from the
first, and a grammar that admits a string the validator rejects is a
*false reject*: the decoder produces its only legal output and the boundary
throws it away. So the renderer's input is the very schema object the validator
reads. The surface type is consulted only for the admission gate in section 4,
which is about a property the schema has already erased.

**The renderer has no permissive fallback.** `_render` dispatches on schema node
shape and raises on a shape it does not recognise. It never emits a catch-all
rule. A wildcard alternative in a decoding grammar is the same failure as a
silent downgrade: it reads as a constraint and accepts everything. A raise here
surfaces as an `internal:` diagnostic naming renderer/predicate drift, which is
the same shape item 257 uses for its own `has_revl_stub` backstop.

## 4. What has no grammar

Two gates, in order, both at compile time, both fail-closed. A type that passes
both gets a grammar. A type that fails either is refused with the offending
position named. There is no third outcome and in particular no runtime
downgrade to an unconstrained decode.

### 4.1 Gate one: `fully_expressible` (item 257, inherited unchanged)

A type with no exact schema has no grammar either, and this gate is not widened
here. That is a deliberate choice against a real temptation. A context-free
grammar *can* express a recursive type (recursion is what a CFG is for),
whereas item 257's inline schema cannot, and refuses it. The grammar domain is
therefore naturally **wider** than the schema domain. Using the wider domain
would admit a response type whose grammar is derivable and whose validator then
refuses every value the decoder produces. Lifting recursion is a change to both
derivations at once (section 7), not a place where one derivation runs ahead.

So the refusals item 257 already names carry over verbatim: an unknown nominal,
an untagged `Result[T, E]`, a `Map[K, V]` with a non-`Str` key, and any type on
a cycle.

### 4.2 Gate two: a null-ambiguous `Opt`

A type can have an exact schema and still have no usable grammar, because a
grammar is judged on the strings it accepts and a schema on the values it
validates. The case that reaches the surface today is an `Opt[T]` whose `T`
already accepts the JSON token `null`: `Opt[Unit]`, `Opt[Opt[U]]`, and either
of those nested inside a list, a map, a record field or a variant payload.

Its grammar derives the string `null` twice, from two different values. A
constrained decode that emits `null` has satisfied the grammar and still has
not said which revl value it meant. The guarantee "the decoder cannot emit
anything the type does not accept" survives; the guarantee a caller actually
wants, "the decoded string names one value", does not.

Item 257 cannot see this, and the reason is worth stating plainly:
`json_schema_for` renders `Opt[T]` as `{**inner, "nullable": true}`, so
`Opt[Opt[Str]]` and `Opt[Str]` derive **the same schema object**. The outer
layer is gone before the validator ever looks. That is why this gate walks the
surface type rather than the schema, and why it has to exist as its own gate
rather than as a check on the derived output.

The refusal names the position and the fix:

```
`validated` emission `complete` has response type `Opt[Opt[Str]]`, which
reaches `Opt[Opt[Str]]`, whose grammar derives the string `null` from both
`Opt[Opt[Str]]` and `Opt[Str]`, so a constrained decode of `null` does not
name one value (flatten it, or wrap the inner type in a named tagged variant)
```

Both fixes are real. `Opt[Str]` is what most authors meant. A `Present(T) |
Absent` variant is what an author who genuinely needs two levels wants, and it
renders as a tagged union with no ambiguity at all.

### 4.3 No new guarantee code

The refusal is `G4`, category `validated`, the same code and category item
257's sibling refusals already use, and it is raised at the same place they
are. `G4` is the right home: this is a rule about what a boundary crossing may
declare, checked at admission, and the fix is always a change to the
declaration. Registering a `G-GRAMMAR` would add a code whose only members are
these refusals, would need its own tier-matrix acknowledgement, and would split
one author-facing rule ("a validated crossing's response type must be one the
boundary can pin") across two codes for no gain to the author being refused.

## 5. The rendering, and the one narrowing it imposes

The derivation is a memoised walk over the schema. Sub-schemas are keyed by
their canonical JSON, so a shape reached twice produces one rule: a grammar is
proportional to the *distinct* shapes in a response type, not to its size.
Lexical rules (`string`, `integer`, `number`, `ws`, base64) are emitted only
when the walk reaches them, so a `-> Str` crossing does not carry the rules for
objects it can never produce.

One decision is visible on the wire. **A record's members are emitted in a
pinned order.** JSON objects are unordered and a grammar that admits every
permutation of `n` members has `n!` alternatives, which is not a grammar
anyone can compile at `n = 8`. The derivation therefore pins one order: the
order the schema presents its properties in, which is the order the fields were
declared.

This narrows what the decoder may produce. It does not narrow what the
validator accepts, and the asymmetry is the point: a provider that honours the
grammar produces canonically ordered members and is accepted, and a provider
that ignores the grammar produces whatever it likes and is validated exactly as
it was before. There is no ordering for which an honoured grammar yields a
refused value, so the narrowing cannot become a false reject.

Two smaller choices in the same spirit. The string rule excludes raw control
characters, because a literal newline inside a JSON string is not legal JSON
and a grammar that allowed one would let a constrained decode produce a value
`json.loads` rejects. And whitespace is permitted between every token, so a
provider that pretty-prints is not refused for it.

## 6. Slices

**Slice 1 (LANDED).** `src/revl/decode_grammar.py`: the grammar-side admission
gate, the GBNF rendering from a derived schema, the digest. `lower.py` runs the
gate and binds `response_grammar` at every `validated` crossing, service
operation and extern alike. `tests/test_decode_grammar_513.py` holds the
derived grammar to accepting exactly what item 257's validator accepts, using a
miniature GBNF recogniser so the property is checked rather than asserted.

**Slice 2, the provider seam (LANDED).** A provider that can constrain a decode
reads `response_grammar` and passes it down; one that cannot ignores it. The
seam is per tier and starts with `backends/python`, whose `_revl_validate` path
already has the crossing's IR in hand. Nothing in slice 2 changes what the
compiler derives. Section 9 is the seam, and section 11 is what a real provider
did with it.

**Slice 3, `model.json[T](prompt)` (NOT landed).** The surface the issue
sketches. It is a sugar over a `validated` emission whose return type is `T`,
and it is deliberately last: the derivation and its refusal are the checkable
part, and adding syntax before them would have shipped a keyword with nothing
behind it. The one thing it adds that the modifier does not is a call site where
`T` is written at the *use*, which needs the return type to be inferred from the
type argument rather than read off the declaration.

Calling it a sugar undersells it, and the cost is worth writing down before the
next attempt starts. Today a service method cannot be generic at all: `service
Model { emission validated fn json[T](h: Str) -> T }` is refused by the parser,
which expects `(` where the `[` is. So slice 3 needs, in order:

1. type parameters on a service-method declaration, and a type argument at an
   `emit` call site, in the parser;
2. unification of the call-site type argument into the return type in the
   checker, since `T` appears in no argument and cannot be inferred from one;
3. and then the part that is not sugar: `response_schema` and `response_grammar`
   are derived in `_validated_response_ir` from the method's *declared* return
   type and live on the method IR. With `T` supplied per use, two call sites of
   one method state two different grammars, so **both keys move from the method
   to the crossing**. That is an IR-shape change, which means all six emitters'
   validate seams read them from the call node rather than from the method spec,
   plus the goldens, the gate crate and the self-host ports.

The honest summary is that slices 1, 2 and 4 are the derivation, the seam and
the dialect, and slice 3 is a language feature that happens to use them.

**Slice 4, dialect negotiation (LANDED).** A second `format` for a provider
whose structured-output mode is JSON-Schema-shaped rather than GBNF-shaped. It
is an added value under the existing key, not a reinterpretation of these bytes,
and item 515's device profile is where "does this member support constrained
decoding" belongs, as section 9 of `docs/design/531-model-placement.md` already
says. Section 10 is the dialect. It landed with slice 2 rather than after it
because the measurement in section 11 could not be taken without it: the
endpoint available to measure against honours a JSON Schema and ignores a GBNF
grammar, so a seam that spoke only GBNF had no real provider to be tested by.

## 7. Recursion, and what would have to change together

The most valuable thing a grammar can express that an inline schema cannot is a
recursive type, and recursive response types are common in exactly the domain
this item serves: a syntax tree, a nested plan, a `Json` value. Slice 1 does not
take it, because taking it on the grammar side alone produces false rejects
(section 4.1).

Lifting it means three changes made together: `json_schema_for` emits `$ref` and
`$defs` instead of refusing a cycle, `fully_expressible` admits a cycle it can
name, and the runtime validator resolves `$ref`. The grammar side needs nothing
new, since a recursive rule is the natural output of a memoised walk over a
cyclic schema. That is the clearest evidence that the schema is the narrow half
and the right place for the work.

## 8. Things stated here that are not verified

**That the emitted GBNF parses in llama.cpp.** The agreement tests use a
recogniser written for this file against the subset of GBNF the derivation
emits. That establishes the language is right; it does not establish that
llama.cpp's own parser accepts the same text. A conformance check against a real
GBNF parser is the honest way to close this, and it is still not done. Section
11 measured an endpoint that ignores a GBNF grammar outright, so it did not
close this either.

**The emission-budget claim.** Section 1 says a constrained decode spends fewer
retries. Section 11 measured that a constrained decode produced a valid response
on every sample and an unconstrained one did not, which is the mechanism, but it
did not run the crossings under a declared `retry N` and count re-issues. No
before-and-after retry counts are reported here, and the roadmap item should not
be read as claiming any.

**That the pinned member order is acceptable to every decoder.** Section 5
argues it cannot cause a false reject, which is a property of the two revl-side
derivations and is tested. Whether a particular decoder finds a fully ordered
object grammar easy to compile, or whether the ordering degrades sample quality,
is a provider question this note still does not answer. Section 11 observed one
provider's ordering behaviour on one model, which is an observation and not an
answer.

**That the honoured-check is sufficient.** Section 9.5 states the opposite
outright: it is necessary and not sufficient, and says exactly what it cannot
see.

What this section used to say, and no longer does: that nothing in the item
observes a decoder. Section 11 does.

---

## 9. The provider seam (slice 2)

Slice 1 stated a grammar and nothing read it. This section is the half that
lets a provider receive it, and the half that decides what happens when one
says it honoured the grammar and did not.

### 9.1 Three calls, and the line between them

The seam is three runtime entry points. In the python tier they are
`runtime.revl_decode_grammar`, `runtime.revl_constrain` and the `grammar`
parameter of the existing `runtime.validate_response`.

```
revl_decode_grammar(key)            -> the stated constraint, or None
revl_constrain(key, dialects)       -> (dialect, artifact, digest), or None
validate_response(..., grammar=key) -> the existing seam, now also judging
```

`key` is the crossing's identity, `"Service.method"`. The emitted module
registers every validated crossing's grammar once at import (`register_grammars`),
so a provider finds the constraint for the crossing it is about to serve without
the compiler having to thread it through a call signature it does not own. A
document with no validated crossing emits no registry and is byte-identical to
one compiled before this slice.

The line between the first two calls is the whole design. **Reading is not
taking.** A provider that only wants to look at the grammar -- to log it, to
decide whether it can honour it, to cache a compiled artifact under its digest
-- calls `revl_decode_grammar` and has promised nothing. A provider that calls
`revl_constrain` is saying something much stronger: *I constrained this decode
with exactly this artifact*. There is no separate declaration API and no
capability flag, because taking the artifact is the only way to use it and is
therefore the only place a claim can be made by accident-proof construction.

A validated **extern** is deliberately not registered. Its `@py` body is the
provider, so registering it would let that body take the constraint -- but this
tier validates a service-method crossing and not an extern's return, so the
claim would never be judged. An unjudgeable claim is worse than no claim.

### 9.2 The decision: is an ignored grammar refused

It is not, and it must not be. A provider that never takes the constraint is
validated exactly as item 257 already validated it, gains no new way to be
refused, and the crossing carries no claim that anything was constrained.

This is not timidity, it is the same rule as section 5. The grammar's language
is item 257's schema language narrowed by a pinned member order. Holding *every*
provider to the grammar would turn a schema-valid completion whose members
arrived in another order into a refusal -- a false reject, of a value the
declared type accepts, produced by a provider that was never told it had to do
anything. The caller's guarantee was never "the decoder was constrained"; it was
"a response that is not of this shape does not reach the body", and refusing
that response would break the second guarantee in order to pretend to the first.

What the sharp version of the question is really asking is different: *once a
seam exists, can the IR claim a constraint nobody enforced?* It can, and that is
where the answer is fail-closed rather than fail-open:

**A claim that was made is checked.** A provider that took the constraint and
returned a completion outside it is a named refusal, `GrammarNotHonouredError`,
and not an accepted value. So an ignored grammar is not a silent downgrade
because nothing was claimed, and a claimed grammar is not a silent downgrade
because the claim is verified. The only remaining fail-open shape -- a provider
that constrains the decode, claims nothing, and is believed anyway -- does not
exist, because nothing downstream is told the decode was constrained unless a
claim was made.

Two claims are refused, and they are different failures:

* **the wrong grammar.** The provider names a digest this crossing does not
  state. This is worse than not constraining at all: a caller reading the
  crossing as pinned would be reading it as pinned to a type it was not pinned
  to.
* **the stated grammar, not honoured.** The provider names this crossing's
  digest and the completion is outside the language. Section 9.4 is what "outside"
  means in practice.

`GrammarNotHonouredError` subclasses item 257's `ResponseValidationError`, which
is not a shortcut. It is a response fault of the same kind and the same
retryability -- a re-issued completion may well be honoured -- so it rides the
existing `retry N` loop, re-issues only the completion, and surfaces the same
terminal typed fault on exhaustion. What the subclass adds is a *name*: "the
provider said it constrained this decode and it did not" is a different
operational problem from "the model answered badly", and a diagnostic that
cannot tell them apart sends the reader to the wrong place.

The schema check runs first. A provider that took the constraint and returned
prose has two problems, and naming the second one first would be unhelpful.

### 9.3 What a provider integration looks like

A provider is the host object bound to the crossing's require key. Constraining
a decode is three added lines, and *not* constraining it is zero:

```python
from runtime import revl_constrain

class OllamaModel:
    def complete(self, prompt):
        body = {"model": self.tag, "messages": [...], "stream": False}
        taken = revl_constrain("Model.complete", ("json-schema", "gbnf"))
        if taken:
            dialect, artifact, _digest = taken
            body["format" if dialect == "json-schema" else "grammar"] = artifact
        return self.post("/api/chat", body)
```

`dialects` is in the provider's order of preference, and the seam hands back the
first one this crossing can supply. A provider that cannot honour any of them
passes a tuple the crossing does not match, or does not call `revl_constrain` at
all, and is verified exactly as it was before.

The trap the seam is shaped to avoid is the third line being written and the
fourth not: taking the artifact and then failing to attach it to the request.
That is precisely the case section 9.2 turns into a named refusal.

### 9.4 A claim is spent by the completion it was made for

The claim register is fiber-local and cleared unconditionally when the response
settles, on the success path and on the schema-failure path alike. A claim
cannot outlive the crossing it was made for and be spent on the next one, and in
a retry loop each attempt makes its own.

### 9.5 What the check can see, and what it cannot

The provider hands revl a **decoded value**, not the bytes it decoded.
Whitespace, number spelling and string escaping are gone before the seam runs,
and no recogniser can recover them. What is *not* gone is member order, because
a JSON object's key order survives decoding -- and member order is exactly what
the GBNF derivation pins (section 5) and exactly what item 257's validator is
blind to.

So the honoured-check is: every member of a closed object is present, and in the
order the grammar pins, recursively. That is precisely the **delta** between the
two derivations, which is what makes it the only part worth checking separately:
everything else the grammar says about a value, the validator has already said.

It is therefore **necessary and not sufficient**. A provider that constrained
with a different grammar that happens to pin the same order passes this check.
The note states that rather than letting "the grammar was honoured" read as a
proof. Closing it means the provider handing back the raw completion bytes
alongside the decoded value, which is a change to the provider contract in all
six tiers and is not made here.

Two smaller limits, both of which fail in the safe direction and neither of
which is fixed:

* **A claim made in another task is lost.** The claim register is a contextvar,
  and a provider that sets it inside a task it spawned rather than in the one the
  crossing runs in leaves the parent seeing no claim. The completion is then
  verified exactly as an unclaimed one, so a claim is dropped rather than
  invented.
* **The registry is per process, not per document.** `register_grammars` merges,
  so two emitted modules loaded into one process that both declare
  `Model.complete` share the key and the later registration wins. The digest
  check catches a provider that then honours the wrong one, but the provider was
  handed the wrong artifact to begin with. A document-scoped key is the fix and
  is not made here.

---

## 10. The second dialect (slice 4)

### 10.1 It adds no IR

The `json-schema` dialect is a pure function of `response_schema`, which the
crossing already carries. So the second dialect costs the IR nothing: the same
crossing offers `gbnf` from `response_grammar["text"]` and `json-schema` from a
derivation over the schema beside it, and the seam negotiates. "An added value
under the existing key, not a reinterpretation of these bytes" turns out to be
stronger than section 6 promised -- there is no new key at all.

### 10.2 The two dialects must describe the same language

A provider's *choice* of dialect must not change whether a completion is legal.
That takes three rewrites, and leaving any of them out makes the dialects
disagree:

* `{"type": "string", "nullable": true}` becomes
  `{"anyOf": [{"type": "string"}, {"type": "null"}]}`. `nullable` is an OpenAPI
  3.0 keyword and not a JSON Schema one. A converter that does not know it drops
  it, and the resulting constraint cannot emit `null` at all -- a narrowing of an
  `Opt` that refuses the one value the author wrote the `Opt` for.
* an object with `properties` is closed and every property is required. Item
  257's schema closes only the variant arms, so a chatty extra member *inside* a
  nested record validates while the GBNF does not admit it. Handing a provider
  the un-rewritten schema would let an **honouring** provider produce a value
  outside the grammar revl stated, which is the worst of the available bugs.
* `contentEncoding: base64` is dropped and the node stays a plain string. This
  is the one place the dialects genuinely differ: a `Bytes` field is
  base64-constrained under `gbnf` and merely string-constrained under
  `json-schema`. Item 257's validator does not check base64 either, so nothing
  regresses, and it is written down rather than papered over.

The agreement is tested the same way slice 1 tested the GBNF: a corpus holding
both verdicts, on which the wire schema accepts exactly the values whose
canonical rendering the grammar accepts.

### 10.3 The dialect decides what the claim means

The two dialects have different digests, so a claim names the artifact that was
actually used and claiming one while having constrained with the other is
detectable rather than believed.

They are also held to different standards, and that asymmetry is the honest cost
of the second dialect. The `gbnf` artifact is a grammar over strings, so
honouring it implies the pinned member order and the honoured-check applies. The
`json-schema` artifact is a schema over values, and **JSON Schema does not
describe member order**, so a provider that honoured exactly what it was handed
may legitimately return members in any order. Holding it to the GBNF's ordering
would be the same false reject section 5 refuses.

There is still a residue, and skipping it would have been fail-open. The artifact
a `json-schema` provider was handed is the **wire** schema of section 10.2, which
is strictly tighter than the one item 257 validates against: it closes the nested
objects 257 leaves open and requires every member. So a `json-schema` claim is
judged against the artifact that was actually handed over, not against the looser
schema the validator happens to use. Concretely, a chatty extra member inside a
nested record passes item 257's validator, is outside the GBNF, and is outside
the wire schema -- so it is accepted from a provider that claimed nothing and
refused from one that claimed either dialect.

What the `json-schema` dialect does *not* buy is the member order, which is the
only part of the GBNF's language that the wire schema cannot express. A reader
who wants that part verified wants `gbnf`.
