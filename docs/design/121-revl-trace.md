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
        latency   1.842s                  (revl-measured)
        attempts  1 of <= 3               (retry 2; within the 257 static ceiling)
        produced  ToolCalls(2)  -> emit[fs] write_report (#9)
        verified  G9 (model-origin args refused into a granting sink; endorse required)
```

Every field carries its **provenance** inline, because it is the whole safety
story (section 4). `tokens`/`cost` are host-reported and marked unverified;
`latency` and `attempts` are revl-measured/revl-controlled and trustworthy. The
`produced` line links this hop to the downstream emission its response caused,
which is the causal edge item 121 exists to draw.

### 1.2 The machine view (`--json`)

```json
{
  "events": 41,
  "modelHops": [
    {
      "seq": 7,
      "component": "AgentLoop",
      "capability": "model",
      "key": "Model.complete",
      "model": {"id": "openai:gpt-4o-2024-08-06", "provenance": "host-reported"},
      "tokens": {"in": 1204, "out": 88, "provenance": "host-reported"},
      "cost": {"amount": 0.0121, "currency": "USD", "provenance": "host-reported"},
      "latency": {"seconds": 1.842, "provenance": "revl-measured"},
      "attempts": {"count": 1, "ceiling": 3, "provenance": "revl-controlled"},
      "produced": [{"seq": 9, "capability": "fs", "key": "Report.write"}],
      "verifiedBy": ["G9"],
      "promptDigest": {"sha256": "...", "bytes": 512, "provenance": "revl-side-args"}
    }
  ]
}
```

There is deliberately **no `prompt` text field and no `response` text field**.
Section 4 (attack 1, the CRITICAL) is why: capturing the text is the exfiltration
channel. `promptDigest` is a content-free hash+length over the *revl-typed*
emission arguments, never over the host-materialized request string, and it is
present only when those arguments carry no secret/confidential origin.

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

### 2.2 The widened `emit` record

```revl fragment
// why_runtime.make_emit_event gains an optional `llm` payload (additive).
// A non-model emit omits it entirely; a v2 reader that does not model llm
// ignores it, exactly as it already ignores `emit` when it models only
// load/withdraw (why_runtime.py:97).
{
  "v": 2, "seq": 7, "event": "emit", "component": "AgentLoop",
  "capability": "model", "key": "Model.complete", "ts": 1234.56,
  "cause": { ... },
  "llm": {
    "model": "openai:gpt-4o-2024-08-06",     // host-reported
    "tokensIn": 1204, "tokensOut": 88,        // host-reported
    "cost": {"amount": 0.0121, "currency": "USD"},  // host-reported
    "latencySeconds": 1.842,                  // revl-measured (monotonic bracket)
    "attempts": 1, "attemptCeiling": 3,       // revl-controlled (257 retry N+1)
    "producedSeq": [9],                       // revl-derived causal edge
    "verifiedBy": ["G9"],                     // revl-derived from the emission's guarantee
    "promptDigest": {"sha256": "...", "bytes": 512}  // conditional; section 4
  }
}
```

The field provenances are fixed and not host-negotiable:

- **revl-measured / revl-controlled** - `latencySeconds`, `attempts`,
  `attemptCeiling`. revl brackets the call and owns the retry loop, so these are
  as trustworthy as any lifecycle `ts`. `attemptCeiling` equals item 257's static
  `N + 1` multiplier, which gives a free cross-check (section 3.2, the oracle).
- **revl-derived** - `producedSeq`, `verifiedBy`. Computed from the checked
  artifact, not from the host: `producedSeq` is the set of later `emit` events in
  the same component+generation whose value flows from this completion's return
  (the causal edge); `verifiedBy` is the G-rule the emission's declared guarantee
  names (G9 for a model-origin return under the taint checker, item 249).
- **host-reported** - `model`, `tokensIn`, `tokensOut`, `cost`. These come from
  the provider response and revl cannot verify them. They are recorded as data
  and every rendering marks them unverified (section 4, attack 2).

### 2.3 What is NOT captured, and why

The prompt text and the response text are **not** recorded. `promptDigest` is a
hash and byte length over the *revl-typed emission arguments* - the `ctx:
List[Str]` the program passed, which the taint checker can see - and it is
emitted only when a compile-time check proves those arguments carry no `secret`
or `confidential` origin. It is never computed over the string the host `@py`
body actually sent to the provider. This is the load-bearing decision; section 4
attack 1 is the derivation.

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

1. **The causal edge is a span link, not just an attribute.** `producedSeq`
   becomes a `SpanLink` from the model hop to each emission span it caused
   (`revl.link.relation = "model-produced"`), reusing the `SpanLink` machinery
   (`otel.py:76`). A trace UI then draws the arrow from "the model said
   `ToolCalls`" to "the tool ran", which is the item-121 story in one picture.
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
2. **Capture no prompt/response text even from the typed args - only a digest.**
   `promptDigest` is `sha256` + byte length over the typed args, and even that is
   emitted **only when a compile-time check proves the args carry no `secret` or
   `confidential` origin**. Recording the digest is a new sink position, so it is
   added to 256's disclosure sink set (`_ORIGIN_CLASSES` consumers, 256 §7b): a
   `secret`/`confidential` argument reaching the trace-capture position raises
   G-SECRET-FLOW at compile time, same as reaching a model prompt. A hash of a
   short secret is itself a brute-forceable leak, so the digest is suppressed
   entirely (not just refused) whenever the origin check is not clean.
3. **PII in a non-secret prompt is a policy choice, not a safety property.** A
   prompt may hold user PII that carries no `confidential` qualifier. The digest
   (hash + length) discloses neither content nor a reversible fingerprint of it,
   and text is never recorded, so the default trace cannot leak PII text. A
   `--capture-prompts` opt-in that records typed-arg text is possible only behind
   the same 256 sink gate and an explicit policy flag; it is out of scope for
   Slice 1 and deferred until 256's confinement can prove a receiver realm for the
   trace file itself.

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

### Attack 3: a hop that misattributes the tool call it produced

The `produced` edge is the causal claim of item 121 ("the model said
`ToolCalls`, and *this* tool ran because of it"). If it were inferred from
timing - "the next emit after the model hop" - it would misattribute under
concurrency: two components' model hops and tool emissions interleave in one
trace, and the nearest-following emit may belong to a different fiber. Mitigation:
the edge is drawn only within the **same component and generation** and only
where the item-257 validated return value actually flows into the later
emission's arguments (a value-flow edge the emitter already knows, since it
rendered both crossings), never by trace adjacency. When the value flow cannot be
established (a `Str`-returning non-validated model emission whose response is
hand-decoded, item 257 §1), `producedSeq` is omitted rather than guessed - the
same honest-degrade discipline `metrics.py`/`otel.py` apply to a missing `ts` or
`code` (buckets as unavailable, never fabricated). A guessed edge is worse than
no edge because it reads as a proven cause.

### Attack 4: the trace itself as an exfiltration channel

Beyond the prompt (attack 1), the widened record adds `model`, cost, and a digest
to a file that flows to a third-party OTel backend. Two channels remain: (a) the
`model` id string is host-reported free text - a malicious host could stuff data
into it. Mitigation: `model` is length-capped and recorded verbatim as a
host-reported opaque string, never parsed or trusted, so it cannot widen the
channel beyond the bytes an operator already chose to export. (b) The digest is a
covert channel only if it is reversible; a 256-bit sha256 over a
non-secret-origin argument set is not, and the origin gate (attack 1) removes the
one case where it would matter. The OTel export path is unchanged from item 120:
it emits only the attributes `build_spans` produced, and no prompt/response text
attribute is ever populated, so turning on OTel export cannot leak more than the
JSON trace already contains.

### Determinism note

The trace is a recording, not a recomputation, so it is not expected to be
byte-identical across runs - `latencySeconds` and host token counts vary by
nature, exactly as lifecycle `ts` already does (`why_runtime.py:70` calls `ts`
"meaningful only for durations within one run"). What *is* deterministic and
must not vary: the presence/absence of the `llm` object (a function of the static
emission classification), the provenance tags, and whether a `promptDigest` is
present (a function of the compile-time origin check, not of runtime data). The
`--json` document sorts hops by `seq` so a diff of two runs isolates the varying
numbers from the fixed structure.

## 5. The sliced plan

### Slice 1 (smallest landable): the model-hop LLM fields on the existing trace, py tier, with the 256 exclusion enforced

- Widen `why_runtime.make_emit_event` with an optional `llm` payload (additive;
  a non-model emit is byte-identical to today). Bump nothing: it rides schema v2's
  additive discipline.
- Build the `llm` object at the `validate_retry` seam
  (`backends/python/runtime.py:257`), py tier only: `latencySeconds` (monotonic
  bracket), `attempts`/`attemptCeiling` (from the loop and the item-257 static
  `N + 1`), `verifiedBy` (the emission's declared G-rule), and the
  host-reported `model`/`tokens`/`cost` passed through from the completion
  return, each provenance-tagged.
- Enforce the CRITICAL: register the trace prompt-capture position in item 256's
  disclosure sink set so a `secret`/`confidential` typed argument raises
  G-SECRET-FLOW at compile time; emit `promptDigest` (hash + length over
  revl-typed args) **only** when that origin check is clean, and never over the
  host-materialized string. Prove it with a differential test mirroring 256's
  `secret_raise` row: a bound-key prompt refuses; a secret-free prompt compiles
  and yields a digest.
- Extend `otel.build_spans` to flatten the `llm` object onto the emit span with
  GenAI-convention names + provenance attributes (section 3.1).
- Add `revl trace <run.jsonl>` (`--json`, `--component`, `--model`) reading the
  same JSONL, next to `metrics`/`why`; the `--otel` flag delegates to `otel.py`.
- Add the `producedSeq` value-flow edge and the section-3.2 attempt-ceiling
  oracle **only for a validated model emission** (where the value flow is known);
  omit both for a plain `Str`-returning completion (honest degrade).

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
