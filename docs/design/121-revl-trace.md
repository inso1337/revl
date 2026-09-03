# 121: `revl trace` - the causal trace with the model hop as a first-class span

Status: DESIGN (implementation pending). No compiler code changes land with this
note; it designs the slices.

Roadmap: item 121 (`docs/v2.0-roadmap.md:3176`). Builds on the causal lifecycle
trace (`src/revl/why_runtime.py`, `docs/why-runtime.md`), extends the OTel
mapping (item 120, `src/revl/otel.py`), and is the per-decision causal companion
to the aggregate metrics (item 122, `src/revl/metrics.py`). Reconciles with the
typed model boundary (item 257, `docs/design/257-typed-model-boundary.md`,
`backends/python/runtime.py:257`), capability-bound secrets (item 256,
`docs/design/256-capability-bound-secrets.md`), and taint/provenance (item 249,
`docs/design/249-taint-provenance.md`).

## Revision (adversarial review 2026-09-01)

A second adversarial review found one NEW CRITICAL, two HIGHs, and a LOW. All are
doc-level: the surface and slicing change, no code has landed. The corrected model:

- **`producedSeq` is not a causal proof under concurrency (NEW CRITICAL).** The v1
  note claimed the `produced` edge (exported as the OTel `model-produced` SpanLink)
  was exact because it was drawn "within the same component and generation" from a
  value-flow edge the emitter knows. It is not. There is no runtime value-flow
  token: `taint.py`'s runtime tag (Slice B) is deferred and unbuilt, so the emitter
  knows only a STATIC IR-site to IR-site edge. `gen` (`run.py:488,618`) is a
  process-global RELOAD counter incremented in `_emit_module`, not a per-activation
  id, so two concurrent fibers of the SAME component in one generation share it.
  And `make_emit_event` (`why_runtime.py:87`) records
  `{v, seq, gen, event, component, cause, capability, key, ts}` with NO
  fiber/activation discriminator. So with two in-flight hops of one component in one
  generation, the single static edge matches BOTH dynamic completion/emit pairs and
  nothing says which completion fed which tool emit; the only residual signal is
  trace adjacency, the exact reconstruction §4 attack 3 forbids. The wrong prompt
  would be SpanLinked to the wrong tool call and exported to a third-party backend
  as a hard proven edge, breaking why_runtime's "causality is exact because it is a
  checked artifact, not reconstructed from timing" foundation.
  Corrected: (a) `make_emit_event` gains a per-activation `activationId`, since
  `component + gen` cannot separate concurrent same-component instances; (b) the
  real mechanism is a fiber-local "last validated completion seq" register, written
  by the emitter at the item-257 `validate_retry` seam
  (`backends/python/runtime.py:257`) and back-referenced at the downstream emit
  crossing, a per-fiber value-flow token, NOT a reader-side reconstruction from
  `component+gen`; (c) HONEST-DEGRADE by default: `producedSeq` and the
  `model-produced` SpanLink are emitted ONLY when that fiber-local token is present
  AND the activation id matches, and OMITTED (never adjacency-guessed) whenever two
  activations of the component are live; (d) until the token lands, `produced` is
  neither rendered as fact nor exported as a SpanLink. The token mechanism moves
  INTO Slice 1 as its crux; if it cannot land there, `producedSeq` is cut from
  Slice 1 to honest-degrade-only (always omitted).

- **The `promptDigest` was a confirmation oracle (HIGH 1).** An unsalted `sha256`
  plus the EXACT byte length, exported to a third-party backend, is not defeated by
  preimage resistance; the threat is confirmation. An observer who suspects a
  specific prompt (a short code, a known key format, a templated string) hashes
  candidates and compares, and the exact length narrows the search. It is reachable
  for a LAUNDERED secret: a tool returns a `secret`-tainted value, the model echoes
  it, and the model op's return mints a FRESH `model` origin (taint does not carry
  `secret` through the model), so the echoed secret re-enters the next hop's args as
  `model`-origin, origin-clean for the secret/confidential digest gate, and its
  digest plus length is exported. Corrected: the digest is SALTED with a per-run
  secret nonce generated at run start and never written to the trace or any span
  (defeats cross-run confirmation, preserves within-run "same prompt twice"
  equality), and the exact `bytes` is replaced with a coarse bucket. The digest's
  only purpose is within-run dedup, which salting preserves; raw sha256 and exact
  length were never the goal.

- **The attack-1 remedy conflated refusal with suppression (HIGH 2).** The v1 note
  said a secret/confidential arg at the trace-capture position both raises
  G-SECRET-FLOW AND is suppressed. Those conflict, and the refusal is wrong for a
  legal program: `taint.py` legitimately admits a `confidential` value at a declared
  `Secret[T]` receiver (`secret_receivers`, `taint.py:261-264,346-350`;
  `_refuse_confidential`, `taint.py:745`, fires only when the receiver does NOT
  declare `Secret[T]`). A model op whose prompt param is declared `Secret[T]` (a
  model allowed to see confidential data) is a legal program; a hard refusal at the
  trace-capture position would make it uncompilable purely because tracing is on.
  Corrected split: TEXT capture (`--capture-prompts`, deferred) is a real disclosure
  sink and REFUSES with G-SECRET-FLOW when args carry secret/confidential and the
  receiver realm is unproven; the DEFAULT digest path SUPPRESSES (records the hop
  with `promptDigest` absent) and never refuses, so a `Secret[T]`-receiving model op
  still gets a hop entry, just no digest. Fail-closed default: the digest is emitted
  ONLY when taint analysis is engaged AND proves the args carry neither origin;
  unavailable or disengaged analysis suppresses. Secret and confidential origins
  mint unconditionally (independent of `taint_strict`, `taint.py:280,305`), so real
  secrets are always tracked.

- **`latencySeconds` framing softened (LOW).** revl brackets the emission honestly,
  but a malicious `@py` body can MODULATE latency by sleeping inside the bracket, a
  weak covert channel riding in as a "trusted" field. The note now states revl
  measures the bracket honestly but does not bound what the host does inside it.

Validated and kept: the arg-only capture rule closes the G8 host-splice channel;
the response and non-model-emission paths are safe by omission (no text captured
there); OTel export respects suppression (`otel.build_spans` copies only present
fields, `otel.py:261-265`).

The rest of this note is revised in place to match; Slice 1 is re-sliced in §5.

## Revision (Slice 2 implementation, 2026-09-02)

Slice 1 shipped the `producedSeq` surface honest-degraded to always-absent: the
token primitive existed (`revl_note_validated_completion`) but nothing called it,
because the design's own spelling of it could not be wired. Three facts, verified
in source, forced a correction rather than a wiring:

- **The mint site can supply neither argument the token asked for.** It wanted a
  driver-owned activation id and trace seq. `_Driver._seq` is private to `run.py`
  and never handed to the runtime, and the activation id is synthesised only at
  step-back time. A mint from emitted code could only invent a number.
- **The edge pointed the wrong way.** The reader puts `producedSeq` on the model
  hop naming the DOWNSTREAM emission it produced; the token held the completion's
  OWN seq. The only self-consistent driver-side mint was a self-loop, which
  `otel.py` would have exported as a SpanLink pointing at itself.
- **There was no value-flow fact to gate on.** "Last validated completion in this
  fiber" is fiber ADJACENCY. Attributing the next crossing in the fiber to it is
  precisely the guess §4 attack 3 forbids.

The corrected mechanism keeps every safety property above and changes only how
the two halves of the proof are carried:

1. **The identity bridge is `replay.Step.index`,** the one identity that already
   crosses the runtime/driver boundary (assigned by the recorder during forward
   execution, handed to the driver at step-back as `entry["index"]`). The token
   holds the completion CROSSING's step index, not a trace seq, and carries no
   activation id.
2. **The token is keyed by the emitter's static completion SITE,** not by
   recency: with two completions live in one body, "the last one" attributes the
   wrong one. The site id rides the `validate_retry` seam as a trailing optional
   argument; a call site the flow analysis did not reach passes none and mints
   nothing.
3. **The static value-flow fact comes from the emitter,** which is the only place
   that can see a downstream emission's ARGUMENTS read the completion's binding.
   A marked crossing fires through `runtime.revl_produced_emit`, which sets a
   fiber-local marker the recorder consumes and stamps as `detail["producedBy"]`.
   The analysis is a MUST-derive under-approximation: a reassigned name, a
   crossing reading two completions, or a closure body yields no mark, so the
   edge is absent rather than wrong.
4. **The driver back-patches.** It maps step index -> the emit event it recorded
   and, after the walk (crossings are reported newest-first), resolves each
   `producedBy` onto the hop and appends the DOWNSTREAM emission's seq to its
   `producedSeq`. A `producedBy` that resolves to no hop in the activation being
   recorded, or to a hop under a different activation id, draws no edge — this is
   the driver-side activation check that replaces the token's dropped argument.
5. **The activation id gains its per-activation discriminator.** `component#gN`
   is `component + gen`, which the NEW CRITICAL above calls insufficient. Each
   recorded `Timeline` now takes a monotonic activation ordinal, and the driver
   spells the id `component#gN#aK`.

Unchanged by this slice: the digest posture (`revl_prompt_digest` still emits
only when taint analysis is engaged AND the args are proven to carry neither a
`secret` nor a `confidential` origin), the host-reported provenance tags, and the
rule that a suppressed field is simply absent.

## 0. What already exists, and the one thing missing

Three landed facilities already turn a `revl run --trace run.jsonl` recording
into an operational artifact:

- **The causal trace** (`why_runtime.py`). Every lifecycle transition is a JSONL
  event whose `cause` names *who* caused it and *why*, exact because the
  dependency graph is a checked artifact (G2 unique provisions, G3 acyclic), not
  reconstructed from timing. `emit` events (schema v2) already record that an
  emission crossed an irreversible boundary, scoped to a `capability` and an
  emission `key` (`make_emit_event`, `why_runtime.py:87`).
- **The OTel mapping** (`otel.py`). `build_spans` maps each event to a span; an
  `emit` event becomes a plain span carrying `revl.capability` and
  `revl.emission.key` (`otel.py:263-265`). Causality survives as span
  links/parents even in a truncated trace.
- **The metrics rollup** (`metrics.py`). `_emission_metrics` counts emission
  crossings by capability; `_duration_metrics` averages `withdraw.ts - load.ts`.

What is missing is the *content of the model hop*. Today an `emit` for a model
completion is indistinguishable from an `emit` for a filesystem write: it names
the capability and the key and nothing else. But the model completion is the one
emission whose response the whole system then executes (item 257 §1). An agent
run is a chain of these hops, and the operator wants to ask, per hop: which model
answered, how many tokens, what did it cost, how long did it take, how many
validation retries did it burn, which emission did the answer produce, and which
G-rule verified the value before the body dispatched on it. `revl trace` adds
those fields to the model hop and carries them all the way through to OTel.

This is a **field extension on an existing event kind**, not new runtime
semantics. The trace is still written off the hot path by the run driver and
read back post hoc; `revl trace` is a new reader plus a widened `emit` record.

## 1. The surface

```
revl trace <run.jsonl>              # human view: the run as a causal chain of hops
revl trace <run.jsonl> --json       # the machine-readable trace document
revl trace <run.jsonl> --component AgentLoop   # one component's hops
revl trace <run.jsonl> --model      # only model hops (the LLM view)
revl trace <run.jsonl> --otel       # emit through the OTel SDK (delegates to otel.py)
```

It parses with `add_parser("trace", ...)` next to `metrics`/`why`
(`src/revl/cli/parser.py:786`) and dispatches from `__main__.py` next to
`_run_metrics` (`__main__.py:829`). Like `metrics`, it always exits 0: reporting
a run's shape is never a gate.

### 1.1 The human view

A model hop rendered as one entry in the causal chain (`render_chain` in
`why_runtime.py:357` is the shape this mirrors):

```
trace: 3 model hop(s) over 41 trace event(s)

  #7  AgentLoop  emit[model] complete   because the turn loop reached a decision point
        model     openai:gpt-4o-2024-08-06
        tokens    1204 in / 88 out        (host-reported - unverified)
        cost      $0.0121 USD             (host-reported - unverified)
        latency   1.842s                  (revl-measured bracket; host owns its inside)
        attempts  1 of <= 3               (retry 2; within the 257 static ceiling)
        produced  ToolCalls(2)  -> emit[fs] write_report (#9)   (fiber token matched)
        verified  G9 (model-origin args refused into a granting sink; endorse required)
```

The `produced` line prints ONLY when the fiber-local value-flow token is present
and the activation id matches (Revision, §4 attack 3); with two activations of the
component live and no token, it is omitted, never adjacency-guessed. Every field
carries its **provenance** inline, because it is the whole safety story (section
4). `tokens`/`cost` are host-reported and marked unverified; `attempts` is
revl-controlled and trustworthy. `latency` is a revl-measured bracket: revl times
the crossing honestly, but does not bound what the host `@py` body does inside it
(a body that sleeps inflates it), so the number is honest about the bracket, not a
guarantee about the host.

### 1.2 The machine view (`--json`)

```json
{
  "events": 41,
  "modelHops": [
    {
      "seq": 7,
      "component": "AgentLoop",
      "activationId": "AgentLoop#g3#a1",
      "capability": "model",
      "key": "Model.complete",
      "model": {"id": "openai:gpt-4o-2024-08-06", "provenance": "host-reported"},
      "tokens": {"in": 1204, "out": 88, "provenance": "host-reported"},
      "cost": {"amount": 0.0121, "currency": "USD", "provenance": "host-reported"},
      "latency": {"seconds": 1.842, "provenance": "revl-measured-bracket"},
      "attempts": {"count": 1, "ceiling": 3, "provenance": "revl-controlled"},
      "produced": [{"seq": 9, "capability": "fs", "key": "Report.write"}],
      "verifiedBy": ["G9"],
      "promptDigest": {"salted": "hmac-sha256:...", "bytesBucket": "256-1k",
                       "provenance": "revl-side-args"}
    }
  ]
}
```

There is deliberately **no `prompt` text field and no `response` text field**.
Section 4 (attack 1, the CRITICAL) is why: capturing the text is the exfiltration
channel. `promptDigest` is a content-free **salted** digest over the *revl-typed*
emission arguments, keyed by a per-run secret nonce that is never itself written to
the trace or any span, never over the host-materialized request string, and it is
present only when the taint analysis is engaged and proves those arguments carry no
secret/confidential origin (otherwise it is suppressed, section 4 attack 1). The
byte count is a coarse `bytesBucket`, not an exact length, so the digest cannot
serve as a confirmation oracle (section 4 attack 4b). `activationId` discriminates
concurrent fibers of the same component in one generation, because `component+gen`
alone cannot (Revision, §4 attack 3); `produced` is present only when a fiber-local
value-flow token ties this completion to that emission and the activation id
matches, and is omitted otherwise.

## 2. How the LLM fields are captured

### 2.1 Where the model hop is observable

The model completion fires at exactly one runtime seam: `validate_retry` /
`validate_retry_async` (`backends/python/runtime.py:257,287`), the item-257
read-with-a-cost loop that issues the completion thunk (`make_call`) and
validates its response. That seam is the only place that sees, in one scope: the
attempt count (revl controls the loop), the wall time around each `make_call`
(revl can bracket it with `time.monotonic()`, the same clock `_record` already
stamps, `run.py:559`), the retry ceiling `budget + 1` (revl set it, item 257
§5.2), and whatever usage metadata the host `make_call` return carries back from
the provider.

The run driver already records an `emit` event at a crossing site via
`_record_emit` (`run.py:564`). `revl trace` widens the model crossing's `emit`
record with an `llm` sub-object built at this seam. A non-model emission records
no `llm` object, so its record is byte-identical to today's (schema-additive,
the same discipline `metrics.py`/`otel.py` already follow for v2 fields).

**Which crossing the hop belongs to is decided at RECORD time, not at read time**
(item 242). The seam measures the hop, but the driver attaches it while walking a
step-back's `emissionsCrossed`, and that walk reports crossings NEWEST FIRST
(`replay.Timeline.step_back` iterates `reversed(tail)`). A discriminator of the
form "did this fiber observe a completion" is therefore consumed by whichever
crossing the walk reaches first: a run that crossed a completion and then wrote a
file gave the model, token, cost and latency numbers to the write. Fiber-locality
cannot repair that, for the same reason §2.2's NEW CRITICAL gives for
`producedSeq` — it isolates concurrent ACTIVATIONS, and both crossings are in one
fiber. So `replay.Timeline.record_emission` publishes each crossing's identity
(`component` + step index) as it mints the step, the seam binds its observation
to the crossing `make_call` just recorded, and the driver reads the entry for the
crossing it is recording. Two completions in one body are two crossings, hence
two entries: the same finer-than-fiber property `producedSeq` gets from the
activation id. The bound on the pre-fix defect is worth keeping: it could not
forge a `produced` edge, because the back-patch requires the resolved target to
carry an `llm` payload, so a misattached payload degrades to no edge — what was
wrong was the hop itself.

### 2.2 The widened `emit` record

```revl fragment
// why_runtime.make_emit_event gains an optional `llm` payload AND an
// `activationId` (both additive). A non-model emit omits `llm` entirely; a v2
// reader that does not model llm ignores it, exactly as it already ignores
// `emit` when it models only load/withdraw (why_runtime.py:97). `activationId`
// is present on every event so concurrent fibers of one component in one
// generation are separable (gen is a process-global RELOAD counter, run.py:618,
// NOT a per-activation id).
{
  "v": 2, "seq": 7, "event": "emit", "component": "AgentLoop",
  "activationId": "AgentLoop#g3#a1",        // revl-assigned per activation
  "capability": "model", "key": "Model.complete", "ts": 1234.56,
  "cause": { ... },
  "llm": {
    "model": "openai:gpt-4o-2024-08-06",     // host-reported
    "tokensIn": 1204, "tokensOut": 88,        // host-reported
    "cost": {"amount": 0.0121, "currency": "USD"},  // host-reported
    "latencySeconds": 1.842,                  // revl-measured bracket (host owns inside)
    "attempts": 1, "attemptCeiling": 3,       // revl-controlled (257 retry N+1)
    "producedSeq": [9],                       // fiber-token-gated; omitted if ambiguous
    "verifiedBy": ["G9"],                     // revl-derived from the emission's guarantee
    "promptDigest": {"salted": "hmac-sha256:...", "bytesBucket": "256-1k"}  // conditional; §4
  }
}
```

The field provenances are fixed and not host-negotiable:

- **revl-controlled** - `attempts`, `attemptCeiling`. revl owns the retry loop, so
  these are as trustworthy as any lifecycle `ts`. `attemptCeiling` equals item
  257's static `N + 1` multiplier, which gives a free cross-check (section 3.2, the
  oracle).
- **revl-measured bracket** - `latencySeconds`. revl brackets the crossing with the
  monotonic clock honestly, but the field measures the BRACKET, not the host: a
  malicious `@py` body can modulate it by sleeping inside the bracket (a weak covert
  channel). It is honest about what revl timed and makes no claim about what the
  host did inside (section 4, the latency note).
- **revl-derived, fiber-token-gated** - `producedSeq`, `verifiedBy`. `verifiedBy`
  is the G-rule the emission's declared guarantee names (G9 for a model-origin
  return under the taint checker, item 249). `producedSeq` is NOT a static
  `component+generation` match (that cannot separate two concurrent activations of
  one component, the NEW CRITICAL): it is present only when a **fiber-local
  value-flow token** ties this completion's validated return to the later
  emission's arguments and the `activationId` matches, and is OMITTED whenever two
  activations of the component are live. The mechanism: the emitter writes a
  fiber-local "last validated completion seq" register at the item-257
  `validate_retry` seam (`backends/python/runtime.py:257`) and back-references it at
  the downstream emit crossing. This is a per-fiber value-flow token, not a
  reader-side reconstruction from timing or from `component+gen`. Until it lands,
  `producedSeq` is absent (honest-degrade), and `produced` is neither rendered as
  fact nor exported as a SpanLink.
- **host-reported** - `model`, `tokensIn`, `tokensOut`, `cost`. These come from
  the provider response and revl cannot verify them. They are recorded as data
  and every rendering marks them unverified (section 4, attack 2).

`promptDigest` is a **salted** HMAC-SHA256 over the revl-typed args, keyed by a
per-run secret nonce minted at run start and never written to the trace or any
span, with a coarse `bytesBucket` instead of an exact length. Salting preserves
within-run "same prompt twice" equality (its only purpose) while defeating the
cross-run confirmation oracle (section 4, attack 4b); the coarse bucket removes the
exact-length narrowing. It is emitted only when taint analysis is engaged and
proves the args carry no secret/confidential origin, and SUPPRESSED (the hop is
still recorded, just without the digest) otherwise, never refused (section 4,
attack 1).

### 2.3 What is NOT captured, and why

The prompt text and the response text are **not** recorded. `promptDigest` is a
salted HMAC (keyed by a per-run secret nonce) plus a coarse byte bucket over the
*revl-typed emission arguments* - the `ctx: List[Str]` the program passed, which
the taint checker can see - and it is emitted only when the taint analysis is
engaged and proves those arguments carry no `secret` or `confidential` origin;
otherwise it is suppressed (the hop is still recorded), never a compile refusal. It
is never computed over the string the host `@py` body actually sent to the
provider. This is the load-bearing decision; section 4 attack 1 is the derivation.

## 3. The OTel mapping extension (item 120)

### 3.1 The model hop as a span with LLM attributes

`otel.py` already maps an `emit` event to a span carrying `revl.capability` and
`revl.emission.key` (`otel.py:263`). The extension: when the `emit` event carries
an `llm` object, flatten it onto the span as OTel-scalar attributes, following
the OTel GenAI semantic-convention names where they exist so the span lights up
in Grafana/Datadog/Honeycomb as a model call, not an opaque emission.

```revl sketch
// in otel.build_spans, the `emit` branch (otel.py:263), additive:
llm = ev.get("llm")
if llm is not None:
    attributes["gen_ai.request.model"]     = llm.get("model")
    attributes["gen_ai.usage.input_tokens"]  = llm.get("tokensIn")
    attributes["gen_ai.usage.output_tokens"] = llm.get("tokensOut")
    attributes["revl.llm.cost"]            = llm["cost"]["amount"]
    attributes["revl.llm.latency"]         = llm.get("latencySeconds")
    attributes["revl.llm.attempts"]        = llm.get("attempts")
    attributes["revl.llm.attempt_ceiling"] = llm.get("attemptCeiling")
    attributes["revl.llm.verified_by"]     = llm.get("verifiedBy")
    // provenance is not decoration - it rides onto the span so a downstream
    // dashboard cannot silently promote a host number to ground truth.
    attributes["revl.llm.cost.provenance"] = "host-reported"
```

Two rules keep this honest and match otel.py's existing discipline:

1. **The causal edge is a span link, not just an attribute - but only when the
   fiber-local token proves it.** When (and only when) `producedSeq` is present
   (the fiber-local value-flow token tied this completion to the emission and the
   `activationId` matched, §2.2), it becomes a `SpanLink` from the model hop to each
   emission span it caused (`revl.link.relation = "model-produced"`), reusing the
   `SpanLink` machinery (`otel.py:76`). A trace UI then draws the arrow from "the
   model said `ToolCalls`" to "the tool ran". When `producedSeq` is absent (two live
   activations, no token, or the token mechanism not yet built), NO `model-produced`
   SpanLink is emitted: the edge is never adjacency-guessed and never exported as a
   hard proven cause, because a wrong SpanLink shipped to a third-party backend
   reads as proof and is worse than no edge (§4 attack 3). This is the NEW CRITICAL:
   the `model-produced` SpanLink is a checked artifact or it is absent, never a
   reconstruction.
2. **No prompt/response text ever becomes a span attribute.** The GenAI
   convention has optional `gen_ai.prompt`/`gen_ai.completion` attributes; this
   design does not populate them (section 4). The digest may ride as
   `revl.llm.prompt.sha256` when present, never the text.

### 3.2 The oracle, extended: attempts vs the static ceiling

Item 120's spirit and `why_runtime`'s oracle (`why_runtime.oracle`) is
differential: a static prediction checked against the recorded actuality, where
a disagreement is a real defect, not noise. The model hop admits the same move
for free. Item 257 §5.2 pins the retry budget as a **static** `N + 1` multiplier
(a validated `retry 2` completion has a compile-time ceiling of 3 crossings per
activation). The trace records the *actual* attempt count. So:

```revl sketch
// a recorded attempts count that exceeds the static N+1 ceiling is a defect
// in one of the two - the runtime over-retried, or the static count under-counts
// - exactly the differential-oracle contract why_runtime.oracle already states.
if hop.attempts > hop.attemptCeiling:
    defect("attempt-ceiling-exceeded", ...)
```

This is not busywork: it is the one model-hop number revl can check against a
compile-time proof, and it is worth surfacing precisely because tokens and cost
are *not* checkable (section 4, attack 2).

## 4. Determinism, privacy, and the adversarial self-review

The trace is a durable, shippable artifact (a file, then OTel spans in a
third-party backend). Everything below treats it as such: what lands in it leaves
the machine.

### Attack 1 (CRITICAL): a secret or PII in the prompt field re-opens the one leak 256 documents as uncatchable

The obvious design records the prompt text so an operator can see what the model
was asked. Item 256 already refuses a `secret` (provider key) or `confidential`
(`Secret[T]`) value at a **model-prompt sink, a log, and an ordinary
serialization** (256 §7, `docs/design/256-capability-bound-secrets.md:72-77`).
The trace is a log and a serialization, so a naive `prompt` field is a new
disclosure sink - and if it is not registered as one, a `secret`/`confidential`
value slips into it *without* the compile-time refusal 256 built, silently.

Worse, there is a residual 256 explicitly documents and cannot catch: the **G8
host-body splice**. A first-party `@py` extern body may build its request string
itself - `prompt = f"sys {openai_key}"` (256 §7, lines 41/223/573) - and revl
never sees that Python f-string. 256 states plainly that this "correctly compiles
and runs, documented as the residual." If `revl trace` captures the prompt by
reading *what the host actually sent* (the materialized request), it captures the
post-splice string containing the key, and no static check can stop it, because
the splice happened in opaque host code. **The trace would become the automated
exfiltration channel for exactly the one leak 256 proved it cannot prevent
statically** - and worse than the residual, because the residual leaks to the
provider the key already authenticates against, whereas the trace leaks it into a
file and a third-party observability backend that was never in the trust
boundary.

This is the CRITICAL, and it forces the capture rule:

1. **Never capture the host-materialized request string.** The trace reads only
   the *revl-typed* emission arguments (the `ctx: List[Str]` the program passed),
   which the taint checker can see and reason about. The G8 splice is invisible to
   the trace by construction, so the residual stays confined to the provider call
   and never reaches the file.
2. **Separate the text sink from the digest path - refuse the first, suppress the
   second.** These are two different positions with two different correct answers,
   and the v1 note wrongly conflated them (HIGH 2):
   - **Text capture** (`--capture-prompts`, deferred) records typed-arg text and is
     a real disclosure sink. It is registered in 256's disclosure sink set
     (`_ORIGIN_CLASSES` consumers, `taint.py:66`, 256 §7b) and **REFUSES** with
     G-SECRET-FLOW when the args carry a `secret`/`confidential` origin and the
     receiver realm is unproven, same as any other disclosure sink.
   - **The default digest path** must **SUPPRESS, never refuse**: when the args
     carry a secret/confidential origin it records the hop with `promptDigest`
     ABSENT. This is load-bearing correctness, not caution: a `confidential` value
     is LEGITIMATELY admitted at a declared `Secret[T]` receiver (`secret_receivers`,
     `taint.py:261-264,346-350`; `_refuse_confidential`, `taint.py:745`, fires only
     when the receiver does NOT declare `Secret[T]`). A model op whose prompt param
     is declared `Secret[T]` is a legal program; a hard G-SECRET-FLOW at the
     trace-capture position would make it uncompilable purely because tracing is on.
     Suppression keeps that program compiling - the hop is still traced, just
     without a digest.
   - **Fail-closed default.** The digest is emitted ONLY when taint analysis is
     engaged AND proves the args carry neither origin. If the analysis is
     unavailable or disengaged, the flow is treated as unproven and the digest is
     suppressed. Secret and confidential origins mint UNCONDITIONALLY (a bound
     emission's return is minted `secret`, a `Secret[T]` return `confidential`,
     independent of `taint_strict`, `taint.py:280,305`), so a real secret is always
     tracked and thus always suppresses, even when the derived-sink strict mode is
     off. And even when emitted, the digest is salted and coarse-bucketed (attack
     4b), so a hash of a short secret is not itself a brute-forceable leak.
   - **The compile-to-runtime taint-origin channel (item 444).** Slice 1 shipped
     the gate with nothing feeding it: `run.py` passed `taint_engaged=False`
     unconditionally, so the digest was suppressed on every real run and the
     surface was inert. The channel is now the IR document the driver already
     holds, read back by `taint.OriginIndex`; no new IR key exists, so every
     golden document is unchanged. It answers the gate's two questions
     separately. `arg_origins` is the checker's own record of the origins that
     reach an emission crossing in that component (`comp["taint"]["reaches"]`,
     item 249 Decision 5), a union over the component's crossings and therefore
     an over-approximation of any one of them, which can only over-suppress.
     `taint_engaged` is a WHOLE-PROGRAM certificate: true only when the
     composition declares no surface that can mint `secret` or `confidential`
     anywhere (no bound secret, no `Secret[T]` parameter or return on any
     extern, service operation or top-level fn, no `Secret[T]` config field),
     which `extract_and_normalize` shows are the only places either origin
     enters the value graph. It is deliberately not a per-crossing judgment: in
     the refusal pass an unqualified parameter is seeded CLEAN, so a
     per-crossing origin set under-approximates across a call boundary, and a
     fail-closed gate must not rest on an under-approximation. A composition
     that declares any confidentiality surface certifies false and keeps every
     one of its crossings suppressed, exactly as before. Both halves must hold,
     so `revl_prompt_digest`'s own origin check still runs on real checker
     output rather than a constant.
3. **PII in a non-secret prompt is a policy choice, not a safety property.** A
   prompt may hold user PII that carries no `confidential` qualifier. The salted
   digest (keyed HMAC + coarse bucket) discloses neither content nor a reversible
   fingerprint of it, and text is never recorded, so the default trace cannot leak
   PII text. A `--capture-prompts` opt-in that records typed-arg text is possible
   only behind the text-sink refusal gate above and an explicit policy flag; it is
   out of scope for Slice 1 and deferred until 256's confinement can prove a
   receiver realm for the trace file itself.

### Attack 2: token and cost numbers are host-reported and unfalsifiable

`tokensIn`/`tokensOut`/`cost`/`model` come from the provider response. A
compromised or buggy host body can report 1 token for a 100k-token call, or the
wrong model id, and revl cannot detect it - there is no on-device ground truth
for a provider's billing. Treating these as verified would let a cost dashboard
built on the trace be trivially gamed, and would falsely imply revl *attests* the
spend. Mitigation: every host-reported field carries `"provenance":
"host-reported"` in the JSON, the human view prints `(host-reported  - 
unverified)`, and the OTel span stamps `revl.llm.*.provenance = host-reported`.
The trace never sums host costs into a "verified spend" figure; item 122's
`metrics` likewise reports counts it *observed*, not spend it *guarantees*. The
one number revl *can* stand behind - the attempt count against the static `N + 1`
ceiling (section 3.2) - is exactly the one marked revl-controlled, so the
provenance split is honest, not cosmetic.

### Attack 3 (NEW CRITICAL): `producedSeq` as a false causal proof under concurrency

The `produced` edge is the causal claim of item 121 ("the model said
`ToolCalls`, and *this* tool ran because of it"), exported as the OTel
`model-produced` SpanLink. The v1 note claimed it was exact because it was drawn
"within the same component and generation" from a value-flow edge the emitter knew.
That is false, and the mistake matters because a wrong SpanLink shipped to a
third-party backend reads as a hard proven cause.

Why the v1 rule does not hold:

- **No runtime value-flow token exists.** `taint.py`'s runtime tag (Slice B) is
  deferred and unbuilt, so the emitter knows only a STATIC IR-site to IR-site edge,
  not a dynamic one tying a specific completion value to a specific later emit.
- **`gen` cannot separate concurrent fibers.** `gen` (`run.py:488,618`) is a
  process-global RELOAD counter incremented in `_emit_module`, not a per-activation
  id, so two concurrent fibers of the SAME component in one generation share it.
- **The record has no fiber discriminator.** `make_emit_event`
  (`why_runtime.py:87`) records `{v, seq, gen, event, component, cause, capability,
  key, ts}` with nothing that separates two activations.

So with two in-flight hops of one component in one generation, the single static
edge matches BOTH dynamic completion/emit pairs, and nothing but trace adjacency
remains to pick one - the exact reconstruction this attack forbids. The wrong
prompt gets SpanLinked to the wrong tool call.

Corrected mitigation (the fiber-local value-flow token, moved into Slice 1):

- **`activationId`.** `make_emit_event` gains a per-activation id, present on every
  event, so concurrent same-component activations in one generation are separable.
- **A fiber-local "last validated completion seq" register.** The emitter writes it
  at the item-257 `validate_retry` seam (`backends/python/runtime.py:257`) when a
  completion validates, and back-references it at the downstream emit crossing. This
  is a per-fiber value-flow token minted by the emitter, NOT a reader-side
  reconstruction from `component+gen` or from timing.
- **Honest-degrade by default.** `producedSeq` (and thus the `model-produced`
  SpanLink) is emitted ONLY when that token is present AND the `activationId`
  matches. Whenever two activations of the component are live and the token cannot
  disambiguate - or for a `Str`-returning non-validated model emission whose
  response is hand-decoded (item 257 §1), where no token is minted - `producedSeq`
  is OMITTED, never guessed. This is the same honest-degrade discipline
  `metrics.py`/`otel.py` apply to a missing `ts` or `code` (buckets as unavailable,
  never fabricated). A guessed edge is worse than no edge because it reads as a
  proven cause. Until the token mechanism lands, `producedSeq` is always absent and
  `produced` is neither rendered as fact nor exported as a SpanLink.

### Attack 4: the trace itself as an exfiltration channel

Beyond the prompt (attack 1), the widened record adds `model`, cost, and a digest
to a file that flows to a third-party OTel backend. Two channels remain: (a) the
`model` id string is host-reported free text - a malicious host could stuff data
into it. Mitigation: `model` is length-capped and recorded verbatim as a
host-reported opaque string, never parsed or trusted, so it cannot widen the
channel beyond the bytes an operator already chose to export.

**(b) The digest is a confirmation oracle unless it is salted (HIGH 1).** The v1
note called a raw `sha256` safe because it is not reversible. Reversibility is the
wrong threat. The threat is CONFIRMATION: an observer who suspects a specific
prompt - a short code, a known API-key format, a templated string - hashes
candidates and compares against the exported digest, and the EXACT byte length the
v1 record carried narrows the candidate set. This is reachable even past the attack
1 origin gate via a LAUNDERED secret: a tool returns a `secret`-tainted value, the
model echoes it, and the model op's RETURN mints a FRESH `model` origin (taint does
NOT carry `secret` through the model, item 249), so the echoed secret re-enters the
next hop's args as `model`-origin - origin-clean for the secret/confidential digest
gate, which therefore never fires - and its digest plus exact length is exported to
a third-party backend.

Mitigation: SALT the digest with a per-run secret nonce minted at run start and
never written to the trace or any span (an HMAC keyed by that nonce). This defeats
cross-run confirmation - an observer cannot precompute candidate hashes without the
nonce - while preserving the digest's only purpose, within-run "same prompt twice"
equality (identical args under one run hash identically). Replace the exact `bytes`
with a coarse `bytesBucket` so length no longer narrows the search. Raw sha256 and
exact length were never the goal; within-run dedup survives salting intact.

The OTel export path is unchanged from item 120: it emits only the attributes
`build_spans` produced (`otel.py:261-265` copies only present fields, so a
suppressed digest never appears), and no prompt/response text attribute is ever
populated, so turning on OTel export cannot leak more than the JSON trace already
contains.

### Determinism note

The trace is a recording, not a recomputation, so it is not expected to be
byte-identical across runs - `latencySeconds` and host token counts vary by
nature, exactly as lifecycle `ts` already does (`why_runtime.py:70` calls `ts`
"meaningful only for durations within one run"). The salted digest VALUE now also
varies across runs by design (the per-run nonce differs), while remaining stable
WITHIN a run so within-run dedup holds; this is the intended behaviour, not a
regression. What *is* deterministic and must not vary: the presence/absence of the
`llm` object (a function of the static emission classification), the provenance
tags, and whether a `promptDigest` is present (a function of whether taint analysis
is engaged and the origin check is clean, not of runtime data). Whether
`producedSeq` is present is a function of the fiber-local token and the
`activationId` match at capture time, not of trace adjacency, so it does not depend
on interleaving order. The `--json` document sorts hops by `seq` so a diff of two
runs isolates the varying numbers from the fixed structure.

## 5. The sliced plan

### Slice 1 (smallest landable): the model-hop LLM fields on the existing trace, py tier, with the corrected causal token, salted digest, and suppression path

- **Widen `why_runtime.make_emit_event`** with an optional `llm` payload AND an
  `activationId` on every event (both additive; a non-model emit carries no `llm`
  and is otherwise byte-identical modulo the new id). It rides schema v2's additive
  discipline; bump nothing. `activationId` is the fix for the NEW CRITICAL:
  `component+gen` alone cannot separate concurrent same-component fibers, because
  `gen` is a process-global reload counter (`run.py:618`).
- **Build the `llm` object at the `validate_retry` seam**
  (`backends/python/runtime.py:257`), py tier only: `latencySeconds` (monotonic
  bracket, provenance `revl-measured-bracket` - honest about the bracket, silent
  about the host's inside), `attempts`/`attemptCeiling` (from the loop and the
  item-257 static `N + 1`), `verifiedBy` (the emission's declared G-rule), and the
  host-reported `model`/`tokens`/`cost` passed through from the completion return,
  each provenance-tagged.
- **The corrected `producedSeq` (the crux of this slice).** Mint a fiber-local
  "last validated completion seq" register at the `validate_retry` seam and
  back-reference it at the downstream emit crossing; emit `producedSeq` (and hence
  the `model-produced` SpanLink) ONLY when that token is present AND the
  `activationId` matches. OMIT it whenever two activations of the component are live
  or the token is absent (a `Str`-returning non-validated completion), never
  adjacency-guess. If this token mechanism cannot land in Slice 1, `producedSeq` is
  CUT from Slice 1 to honest-degrade-only (always absent, no `model-produced`
  SpanLink), rather than shipped as a false proof - `produced` must never be
  rendered as fact nor exported as a SpanLink until the token exists.
- **The SALTED digest and the SUPPRESSION path.** Mint a per-run secret nonce at
  run start, never written to the trace or any span; compute `promptDigest` as an
  HMAC keyed by that nonce over the revl-typed args, with a coarse `bytesBucket`
  instead of an exact length (defeats the confirmation oracle, attack 4b). Emit it
  ONLY when taint analysis is engaged and proves the args carry no
  `secret`/`confidential` origin; otherwise SUPPRESS (record the hop with the digest
  absent), never refuse - so a `Secret[T]`-receiving model op still gets a hop
  entry (HIGH 2). Never compute it over the host-materialized string.
- **The text-sink refusal is separate and deferred.** Register only the deferred
  `--capture-prompts` text-capture position in item 256's disclosure sink set
  (`taint.py:66`) so a `secret`/`confidential` typed argument raises G-SECRET-FLOW
  there; the default digest path does NOT refuse. Prove the split with two
  differential tests: (i) mirroring 256's `secret_raise` row, a bound-key prompt at
  the text-capture sink refuses; (ii) a `Secret[T]`-receiving model op COMPILES and
  yields a hop with the digest suppressed (not a refusal), and a secret-free prompt
  compiles and yields a salted digest.
- **Extend `otel.build_spans`** to flatten the `llm` object onto the emit span with
  GenAI-convention names + provenance attributes (section 3.1), emitting a
  `model-produced` SpanLink only when `producedSeq` is present. It already copies
  only present fields (`otel.py:261-265`), so a suppressed digest and an omitted
  `producedSeq` never appear.
- **Add `revl trace <run.jsonl>`** (`--json`, `--component`, `--model`) reading the
  same JSONL, next to `metrics`/`why`; the `--otel` flag delegates to `otel.py`.
- **Add the section-3.2 attempt-ceiling oracle** (`attempts` vs the static
  `N + 1`), the one model-hop number revl can check against a compile-time proof.

Deferred out of Slice 1:

- **Cross-host distributed trace stitching.** Item 121's title says
  "distributed"; a multi-daemon agent run (worker realms,
  `docs/distribution-model.md`) produces one trace per host that must be stitched
  under one OTel trace id with the causal edges crossing host boundaries. Slice 1
  records and maps a single-host trace; the cross-host join (a shared trace id
  minted at the root emission and propagated as an OTel `traceparent` across the
  realm boundary) is Slice 2, because it needs a propagation contract the
  distribution model does not yet expose.
- **The `ts`/go/java/rs mirror** of the `llm`-payload capture (Slice 1 is py-tier
  only, the same tier-scoping item 256 slice 1 used for the `_revl_secret` seam).
- **`--capture-prompts`** (typed-arg text behind the 256 gate + a policy flag +
  a proven receiver realm for the trace file).
- **Confidence.** Item 121's field list names "confidence"; it is model-self-
  reported and therefore `Untrusted`/host-reported with no ground truth, so it is
  deferred until there is a use that survives attack 2 - recording an
  unfalsifiable self-score as if it meant something is exactly the trap that
  section warns against.
