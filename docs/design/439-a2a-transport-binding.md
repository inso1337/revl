# 439: the A2A 1.0.0 transport binding for remote providers

Item: roadmap 439. Issue: #118. Status: BINDING, landed, last revised 2026-09-13.
The semantics are item 424 gap (c)'s and are not reopened here; the Task
lifecycle is `docs/design/439-a2a-task-lifecycle.md`'s and is not re-decided
here. This note records what the binding IS, where every guarantee still applies
at the boundary, and what is still open.

Every `file:line` below was read off the tree this note is committed from.

| question item 439 must decide | where it is decided |
| ----------------------------- | ------------------- |
| one emission, a stream (item 130), or a session (item 250)? | `docs/design/439-a2a-task-lifecycle.md`, decision 1: stream-shaped, never session-shaped. Question (1) below states the criteria, not just the verdict |
| an Agent Card is a CLAIM, so what does the boundary do about it? | question (2) below, and the modality decision it rests on |
| the claim is "A2A 1.0.0", never "A2A" | question (3) below; `src/revl/import_a2a.py:185` |
| remoteness is an admission fact, not a wiring fact | item 424 gap (c), D-424c.1..D-424c.10; the reuse table below |

## What was already true before this slice

Two entry points onto A2A 1.0.0 already existed:

1. `revl import a2a` (`src/revl/import_a2a.py`, item 439 slice 1). It reads an
   Agent Card and emits revl source: a `service`, one `extern` per skill, and a
   provider component whose `@py`/`@ts` bodies POST an A2A `message/send`
   directly. This is the "I have a card, generate me a client" path. It writes
   the service itself, so it can declare the returns `Untrusted[Str]` and colour
   the ts tier `async`.

2. The `remote` composition row (`src/revl/synthesize.py`, item 424 gap (c),
   slice C2). It synthesizes a provider for a service the composing engineer
   ALREADY wrote, so a `requires key: Service` consumer does not change one
   character when a local provider becomes a remote one. Remoteness is an
   admission fact (a reach, a capability, a failure mode), never a wiring fact.
   It speaks exactly one wire: the placement bridge's canonical envelope,
   `{"key","method","args"}` to `{"ok","value"|"error"}`, selected by omitting
   `through`. A `through <name>` clause was refused for every name, because no
   named wire was bound and shipping the canonical body under a foreign label
   would have been dishonest.

`synthesize.py` reserved `through a2a` for exactly this item. `check_transport`
refused it, and its docstring named item 439 as the binding that would land it.

## The decision: `through a2a` is the wire, and it maps onto the canonical envelope

The remote row is the right home for the transport binding, not a second copy of
the importer. The importer answers "generate me a client from a card"; the
remote row answers "remote a service I wrote onto a peer". This slice binds the
first NAMED wire of the remote row:

> `through a2a` crosses A2A 1.0.0's `message/send`, and it does so by MAPPING the
> canonical seam envelope onto the A2A message shape at the boundary.

Concretely, the mapping (`_py_body_a2a` in `src/revl/synthesize.py:591`; the
modality check that admits each shape is `_check_a2a_method`, `:429`):

| canonical seam envelope          | A2A 1.0.0 `message/send`                            |
| -------------------------------- | --------------------------------------------------- |
| `args[0]`, a `Str`               | `params.message.parts[0]` as `{kind:"text",text}`   |
| `args[0]`, a `Bytes`             | `params.message.parts[0]` as `{kind:"file","file":  |
|                                  | {"bytes":<base64>}}` (`FileWithBytes`, inline)       |
| `method` (the op name)           | `params.message.metadata["revl.skill"]`             |
| reply `value`                    | the text `Part` (or the inline-bytes file `Part`) of |
|                                  | a TERMINAL `Task`/`Message` reply                    |
| reply `{"ok":false,"error"}`     | a transport failure, a JSON-RPC error, a             |
|                                  | non-terminal task, a non-file reply, or a file reply |
|                                  | that carries a `uri` instead of inline bytes         |

The two projected modalities are `Str` and `Bytes` and nothing else
(`_A2A_MODALITY`, `src/revl/synthesize.py:197`); everything else is refused
naming the method, never flattened onto the one `Part` the crossing sends.

The row keeps everything the canonical wire already decides, because it reuses
the same machinery rather than reimplementing it: one crossing, redirect refusal
(`src/revl/crossing_redirect.py`), the `CROSSING_TIMEOUT` deadline, and the
`on_failure(withdraw|result)` branch. Only the payload built and the reply
parsed differ. The version claim and the terminal-state list are imported from
`import_a2a` (`A2A_VERSION`, `_TERMINAL_STATES`) so the two entry points onto the
protocol cannot drift.

## Question (1): a Task is more than one crossing, and it is never a session

This is the question item 439 calls load-bearing and not obvious. The answer is
**neither of the two the single-crossing wire implies**: an A2A Task spans more
than one crossing, so the binding projects the Task lifecycle rather than
pretending the terminal subset is the protocol. It is **never a session** (item
250). The decision of record is
`docs/design/439-a2a-task-lifecycle.md` (status DECISION), decision 1; what
follows is the criteria that force it, so a future reader can re-derive it.

| candidate | the criterion that would have to hold | verdict |
| --------- | ------------------------------------- | ------- |
| one emission | the whole Task reaches a terminal state inside that one crossing | the TERMINAL SUBSET, not the protocol. It is what the default wire binds, and what `revl import a2a` binds |
| a stream (item 130) | the peer changes state more than once and the composition must OBSERVE each change | the shape of the lifecycle. Landed as the four-op projection (slice T1); the `Stream[T]` sugar over it is slice T2 and waits on item 130 |
| a session (item 250) | WE own the timeline: we can rewind it, fork it, and hash-bind a commit to it | REJECTED |

What breaks under each rejected option:

- **One emission as the whole answer.** The second state has nowhere to go. A
  peer that is waiting for an answer (`input-required`) is indistinguishable from
  a peer that is still working (`working`), so the composition can only sit on the
  crossing deadline (`CROSSING_TIMEOUT`, `src/revl/crossing_redirect.py:82`). It
  also makes the calm case dishonest: the body would have to report a
  non-terminal state as success, which is a lie, or as a generic transport
  failure, which throws away a Task that is still running and still addressable
  by `tasks/cancel`.
- **A session.** A session is our own accumulator's timeline: rewind, fork, a
  hash-bound commit, and an honesty model about which of OUR inverses may run. A
  remote Task cannot be rewound, only cancelled (`tasks/cancel`, projected as the
  `compensate` of `_start`, item 247), so binding it to a session claims a
  reversibility the peer never offered. It would also spend a standing approval
  against a timeline that is not ours: an approval is consumed by OUR commit
  (item 246), and a peer's clock is not ours to spend it on.
- **A stream, without the handle form.** The destination is a `Stream[T]` as a
  service operation's return, and item 130 has not admitted that surface yet. So
  the binding lands the form that exists now and is honest about the wire's
  shapes: `long_running` on a row projects four ops, `_start` / `_poll` / `_reply`
  / `_cancel` (`src/revl/a2a_task.py:62`, `stdlib/a2a.rvl`), admitted by
  `_check_long_running` (`src/revl/synthesize.py:755`) and built by `_task_ops`
  (`:795`). Every one of the four returns `Untrusted[T]`, and the reply op exists
  precisely so a state the peer is WAITING on is answerable instead of ignored.

The default wire is unaffected and stays what it was: a `message/send` whose Task
reaches a terminal state in that one crossing, with a non-terminal reply
(`working`, `input-required`, `auth-required`, `unknown`) a FAULT at the boundary
(`src/revl/synthesize.py:746`, and the terminal list is imported from the
importer, `src/revl/import_a2a.py:212`, so the two entry points cannot drift).
`long_running` is admitted only on `through a2a`, and is refused on the default
wire and on `through a2a_rest` (`tests/test_439_a2a_transport.py:731`, `:740`).

## Question (2): the boundary refuses the untrusted author, and `Untrusted[T]` alone is not enough

An external agent is not a revl composition, so nothing about it is checked: not
its Agent Card, not its declared skills, not its reply. The card is a CLAIM the
peer writes about itself. Every A2A provider is therefore item 329's
untrusted-author case by construction, and the boundary can admit nothing about
the callee, because a client is the SENDER (D-424c.8) and item 337 puts the
re-compile on the RECEIVER. The generated header states this in the row's own
language (`src/revl/synthesize.py:1011` and `:1055`).

What the boundary does about it, from the value outwards:

1. **The value channel: `Untrusted[T]`, and it is necessary.** Every synthesized
   crossing declares its return `-> Untrusted[T]` (`src/revl/synthesize.py:936`),
   which `taint.extract_and_normalize` (`src/revl/taint.py:339`) reads as a taint
   source whose origin is the crossing's reach class, so every consumer of the key
   is tainted interprocedurally (D-424c.9). A remote result reaching a `Trusted[T]`
   sink is refused (G9) with no `endorse` and admits with one
   (`tests/test_439_a2a_transport.py:311`, `:325`).
2. **The declaration channel: nothing is admitted, so nothing is assumed.** The
   row carries the peer as an ADDRESS, never as a checked type. `check_address`
   (`src/revl/synthesize.py:1615`) refuses a URL, a path and userinfo, and the
   capability token is folded from the HOST alone (`:221`, D-424c.10). The only
   thing the row asserts about a peer is the protocol version, and it asserts it
   exactly (question (3)).
3. **The crossing channel: an ordinary class-(c) crossing.** The synthesized
   extern is a bare `emission[<cap>]` (`src/revl/synthesize.py:954`), so a remote
   crossing is ticket-bound exactly like a local one (the guarantee table below).
   An untrusted author cannot buy a crossing without the approval a local effect
   would need.
4. **The observation channel, and this is where `Untrusted[T]` is NOT
   sufficient.** A taint is a property of a VALUE the checker can see, and it says
   nothing about the TEXT the boundary renders. The A2A body renders four
   peer-authored fields into the consumer's own fault text: the JSON-RPC
   `error.code` (`src/revl/synthesize.py:688`), the task `state`, twice (`:746`,
   `:747`), and the reply `kind` (`:752`). A peer that reflects what we sent into
   `error.code` puts caller-visible text on our error channel and on the operator
   console, and no `Untrusted[T]` qualifier covers that.

**So `Untrusted[T]` is necessary and not sufficient**, and the concrete extra
mechanism is not a new language feature. It is the funnel item 421 F5 already
built, joined at THIS seam: `confidential.redact_call_text`
(`backends/python/confidential.py:415`), whose own docstring is the shape exactly
("a failure crossing a seam must not hand the consumer back the values it was
called with"). It has one call site in the runtime, the placement seam's failure
path (`backends/python/bridge.py:460`).

**That funnel is now joined here too, and this is slice B1** (`src/revl/
a2a_boundary.py`, `py_funnel`). A synthesized A2A body is a plain `urllib` POST
that never touches `bridge.py`, so the funnel is EMITTED as source rather than
imported, for the reason `revl.crossing_redirect` gives about the redirect
policy: the rule is readable in the file an operator reviews rather than applied
from a runtime import the generated file would have to trust.
`backends/typescript/emit_temporal.py` emits the same funnel into a workflow
module for the same reason. The contract is F5's exactly, including its limits:
an EXACT match against this call's own arguments (never a pattern), longest
needle first, the same `<redacted:arg>` placeholder and the same minimum needle
length (pinned equal to `confidential`'s in
`tests/test_439_a2a_boundary.py::test_the_placeholder_and_the_bound_match_the_seams_funnel`),
and both faces of a `Bytes` argument. The failure's SHAPE survives, which is the
point: the sentence that makes a fault worth reading is ours, and only the
caller's bytes are removed. A message that renders nothing of the peer's is not
funnelled at all, because running the funnel over a sentence that is wholly ours
could only damage our own diagnostic.

B1 joins the funnel on all three generated py bodies at once (the terminal wire,
the four-op wire, and `revl import a2a`), because the three are one boundary and
a gate on one of them only would be a peer read differently per entry point. The
coloured `@ts` importer body is the one place it is NOT joined yet: the py tier
is first there exactly as the file `Part` and the async recolour are, and it is
recorded as remaining below.

Two things make that bounded rather than open-ended, and both are decisions:

- **The modality subset IS the redaction contract.** `through a2a` admits only
  `Str` and `Bytes`, on both sides (`_A2A_MODALITY`, `src/revl/synthesize.py:197`,
  enforced at `:468` and `:490`), and neither is a marked type. So no declared
  `Secret[T]` value can cross this wire in either direction, which is why the F5
  shape (a marked value funnelled through the failure text) has no crossing point
  here. That is the reason the checks are drawn where they are, not an accident of
  them: widening the subset (a `DataPart`, a richer `Part`, a `Secret[T]`
  passthrough) reopens F5 at this boundary and must arrive WITH the funnel. The
  refusal is pinned as a decision in
  `tests/test_439_a2a_transport.py::test_no_marked_value_can_cross_the_a2a_wire`.
  With B1 landed the widening rule is unchanged and cheaper to keep: the funnel
  is already at the boundary, so a wider modality arrives with it rather than
  needing it built.
- **F6 already covers the trace, at a different granularity.** The host trace is
  scrubbed at the one choke point every event passes through,
  `confidential.redact_text` (`backends/python/runtime.py:1198`, item 421 F6),
  which removes every REGISTERED secret value from a rendered event. That is a
  value-registry funnel, not a per-call one: it does not know our arguments, and
  it has nothing to remove when the leaking text is the peer's own choice of
  string. So F6 bounds the blast radius and does not substitute for the per-call
  funnel.

**Verdict, stated so a future PR can be held to it:** `Untrusted[T]` plus G1, the
capability cone, the ticket and F5/F6/F7 is NOT sufficient for item 439's second
question, because every one of those is about values, declarations, crossings or
the trace, and none of them is about text the boundary RENDERS from a peer's
reply. The extra mechanism is the F5 funnel joined at the A2A boundary, with the
modality exclusion held as the precondition that keeps it small. That is what
slice B1 built (above), for the py tier.

### The fifth layer B1 adds: a reply is not a verdict until it correlates

Question (2) asked what the boundary does about a peer whose every claim is
unchecked, and the four layers above answer it for the value, the declaration,
the crossing and the rendered text. Building the fourth made a fifth visible, and
it is the same honesty applied one step earlier: the boundary was reading a reply
it had not established was an answer to ITS OWN request.

Every crossing now carries one correlation identity, a uuid4 that is the JSON-RPC
2.0 envelope `id` AND the `revl.correlation` member of the A2A message metadata
(`a2a_boundary.CORRELATION_KEY`), so the identity the peer logs is the identity
the reply is checked against. A reply is read only after three gates pass, in
order: it is a JSON object, it claims JSON-RPC `2.0`, and its `id` is exactly this
crossing's. Anything else is the crossing's ordinary fault, which
`on_failure(withdraw)` turns into provider withdrawal and `on_failure(result)`
turns into an `Err`.

Three things about the gates are decisions rather than implementation:

- **It refuses only a peer already off protocol.** JSON-RPC 2.0 requires a
  response `id` to equal the request's, so a compliant A2A 1.0.0 peer passes
  without knowing revl exists. The metadata member is additive and namespaced
  like `revl.skill`, so a peer that ignores it is unaffected.
- **The shape is checked before any member is read.** A peer that answers a JSON
  array, a string, `null`, or a truthy non-object `result` used to reach
  `.get(...)` on a non-dict and raise an `AttributeError`, which is a fault
  NEITHER settlement classifies: not a withdrawal, not an `Err`. An unparseable
  reply is now the declared fault like every other refusal on the wire.
- **The refusal renders nothing of the peer's.** The id a peer DID answer with is
  never reported. That is the gate that exists because peer text is a claim, so
  putting peer-chosen text on the error channel inside it would contradict the
  layer above.

The envelope gate is an ENVELOPE gate, so it exists where there is an envelope.
A2A 1.0.0's HTTP+JSON/REST sub-transport replies with the bare `Task`/`Message`
and echoes nothing a client could check, so `through a2a_rest` carries the
identity one-way (on the wire, for the peer's log and ours) and gets the shape
gate alone. A check the wire cannot make is not emitted as if it could.

One layer deeper on the four-op wire: `_poll`, `_reply` and `_cancel` name a task
they already hold, so a reply that describes ANOTHER task is refused
(`py_task_identity_gate`). An envelope-correlated reply about the wrong task is
the same failure one level in, and a `tasks/get` answered with a different task's
status would otherwise be read as a lifecycle event for ours. `_start` mints the
identity, so it has none to check; a `tasks/cancel` acknowledgement that names no
task at all is still honoured, because that op is best-effort by design (item
247).

## Question (3): the version lives in the binding, and a mismatch is refused at admission

The claim is "A2A 1.0.0", never "A2A". Where the version lives, in each surface
that can state one:

| surface | where the version is |
| ------- | -------------------- |
| the one constant | `A2A_VERSION = "1.0.0"` (`src/revl/import_a2a.py:185`) |
| the importer's admission input | the Agent Card's `protocolVersion` (`src/revl/import_a2a.py:317`) |
| the row's emitted header | `// Transport: A2A {A2A_VERSION} over JSON-RPC 2.0` (`src/revl/synthesize.py:1032`; `:1024` for REST; `:1005` for the four-op form) |
| the four-op vocabulary | `src/revl/a2a_task.py:24` |

It is deliberately NOT a type parameter and NOT a declaration clause. There is no
`a2a<1.0.0>` and no `protocol` clause on a remote row, and that is the decision
rather than an omission: `through a2a` MEANS A2A 1.0.0. A version is a property
of a binding, and a binding named after the protocol with a version parameter
would invite exactly the silent widening this note exists to refuse.

What a mismatch does, at each place a version can be stated:

- **A card that claims another version, or none, is refused at admission**, before
  one line of source is emitted. `_version()` (`src/revl/import_a2a.py:317`)
  refuses a missing `protocolVersion` (message at `:321`) and any value that is not
  exactly `1.0.0` (`:329`), with the pointer `#/protocolVersion` and a hint that
  the fix is either the card or a binding for the version it does speak (`:333`).
  Fail-closed, pinned by
  `tests/test_import_a2a.py:183` and `:189`.
- **A remote row cannot mismatch, because it states no version of its own.** The
  row IS the binding, so it can only ever claim the version it is. A peer that in
  fact speaks something else is therefore not an admission-time question on this
  path at all. It surfaces at the first crossing as a protocol-shaped reply, and
  every such shape already faults rather than being guessed at: a `kind` this
  binding does not know (`src/revl/synthesize.py:752`), a `state` that is not
  terminal (`:746`), a reply with no inline file `Part` (`:702`), or a JSON-RPC
  `error` (`:688`). That is the honest settlement: a fault naming the crossing,
  never a compatibility nobody checked.
- **A future version is a new binding, never a widened one.** An `A2A 1.1` would
  be a new `through` name with its own header, its own terminal-state list and its
  own note; `_A2A_TRANSPORTS` (`src/revl/synthesize.py:173`) and the refusal in
  `check_transport` (`:1568`, which names gRPC explicitly) are where that is
  enforced today.

## Reuse: item 424 gap (c), and where each of its decisions lands on A2A

This binding adds no semantics. Item 424 gap (c) decided the semantics and left
the transport abstract on purpose; this is the whole of the mapping from those
decisions onto A2A's concrete shapes.

| decision | what it decided | how the A2A binding lands it |
| -------- | --------------- | ---------------------------- |
| D-424c.1 | a remote provider is a ROW whose provider is synthesized, so no consumer changes | unchanged: `_resolve_remote` (`src/revl/composition.py:781`) synthesizes a provider for the service the engineer wrote, and the A2A-ness is `transport` on the row table |
| D-424c.2 | remoteness is an ADMISSION fact (a reach, a capability, a failure mode), never a wiring fact | unchanged: the reach is `net.<host>`, folded by `cap_token` (`src/revl/synthesize.py:221`) and set on the row (`src/revl/composition.py:809`); the failure mode is `onFailure` on the row |
| D-424c.3 | a transport failure is a WITHDRAWAL by default, with `on_failure(result)` as the opt-in | unchanged, and now complete on both halves: the body raises a marker-bearing `TransportFault` (`src/revl/synthesize.py:654`) and the activation runtime maps it to provider withdrawal (slice T0, `src/revl/run.py:1870`), while `on_failure(result)` returns the `Err` and does not withdraw |
| D-424c.8 | a generated client does NOT re-admit the callee (337: the receiver re-compiles from its own source; a client is the sender) | unchanged, and stated in the emitted header (`src/revl/synthesize.py:1011`, `:1055`) |
| D-424c.9 | every returned value is `Untrusted[T]` | the `-> Untrusted[<T>]` wrapper on the synthesized extern's return (`src/revl/synthesize.py:936`), shared by both wires in the one place they share |
| D-424c.10 | the capability is folded from the HOST alone, because a credential in an address is a live secret, not an identifier | `cap_token` (`src/revl/synthesize.py:221`) behind `check_address` (`:1615`): a URL, a path and userinfo are refused, so a userinfo-bearing URL can never become part of a capability spelling |
| the redirect policy | the peer address IS the address; a redirect is refused by default | reused unchanged from `src/revl/crossing_redirect.py` (`py_policy`, `:85`; the deadline is `CROSSING_TIMEOUT`, `:82`), asserted on this wire by `tests/test_439_a2a_transport.py:190` |

What the binding adds is the WIRE and only the wire: the payload built
(`_py_body_a2a`, `src/revl/synthesize.py:591`), the modality check
(`_check_a2a_method`, `:429`), the Task projection (`_task_ops`, `:795`) and the
header (`_a2a_header_lines`, `:997`).

## What `through a2a` refuses, and why

The binding holds the same honesty line the importer does. Each refusal names
what it refuses:

- **A non-`a2a` `through <name>`.** Still refused by `check_transport`. gRPC in
  particular is a binary HTTP/2 transport with protobuf framing, not the JSON
  POST this synthesizer emits, so it cannot ship under the `a2a` label or any
  other.
- **A method outside the two projected modalities.** A2A `message/send` crosses
  one user message. `through a2a` therefore binds a method of shape
  `emission fn op(m: Str|Bytes) -> Str|Bytes` (or `-> Result[Str|Bytes, Str]`
  under `on_failure(result)`, where the `Err` is always text). More than one
  parameter, a parameter or return that is not `Str`/`Bytes`, or an
  `on_failure(result)` return that is not `Result[Str|Bytes, Str]`, is refused
  naming the method rather than flattened onto the one `Part` the crossing sends
  (`src/revl/synthesize.py:468`, `:490`). A `DataPart` (arbitrary structured
  JSON) is refused for the same reason: it needs the tagged half of the canonical
  encoding, which is item 424 slice C1's to build, so the boundary will not guess
  one. The modality subset is load-bearing beyond marshalling: it is what keeps
  every marked value off this wire (question (2)).
- **A non-terminal task.** See decision (1).

## Every boundary guarantee, and the code that enforces it

Item 439 requires that every existing guarantee still applies AT THE BOUNDARY.
The A2A path is not a parallel mechanism: a remote row synthesizes an ordinary
provider holding ordinary externs, so the ordinary checks see the ordinary
shapes. Each row names the enforcement site in the tree this note is committed
from. Where the rule is about the wire's own shape rather than a mechanism, the
row says so instead of inventing a site.

| guarantee | how it reaches the A2A crossing | enforced today at |
| --------- | ------------------------------- | ----------------- |
| G1, declared-only access | the consumer writes `requires key: Service` and nothing else; the synthesized provider requires nothing of its own and holds one extern per method whose only reach is the folded token | `src/revl/composition.py:809` (the token), `:832` (`requires=[]`), `src/revl/synthesize.py:954` (`extern emission[<cap>]`) |
| the capability cone | the token is a declared token on an ordinary extern, so cone scoping and the ceiling check see it unchanged; the binding adds and removes no token | `src/revl/lower.py:12838` (`_cap_keyed`), `:13180` (`_ceiling_attenuation_check`) |
| tickets, class (c) | the extern is a bare `emission`, so the crossing is class (c) and receives the per-call ticket; an unanswered ticket refuses fail-closed rather than running the crossing | `src/revl/deploy.py:3280` (the class), `src/revl/gate.py:838` (the ticket on the activation body), `src/revl/recovery.py:549` (recovery re-asks, never auto-answers) |
| F5, the call-argument funnel | JOINED at this boundary (slice B1), emitted as source rather than imported because a synthesized body is not a seam client: the peer-authored text the boundary renders is scrubbed of this call's own argument values by exact match, and the marked-value exclusion above still keeps the subset small | `src/revl/a2a_boundary.py` (`py_funnel`), mirroring `backends/python/confidential.py:415` (the runtime funnel) and `backends/python/bridge.py:460` (its seam call site) |
| the correlation identity | every crossing carries one uuid4 as the JSON-RPC envelope `id` and the `revl.correlation` metadata member, and a reply that is not a JSON object, does not claim JSON-RPC 2.0, or does not carry that identity back is the crossing's ordinary fault, never a value. `through a2a_rest` carries it one-way, because a REST reply echoes no envelope | `src/revl/a2a_boundary.py` (`py_correlation`, `py_envelope_gates`, `py_task_identity_gate`) |
| G8, the enumerable boundary | a remote row synthesizes an ordinary provider holding ordinary externs, so the one (or four) synthesized crossings are on the boundary surface with their folded `net.<host>` reach, which is what makes `docs/design/439-a2a-task-lifecycle.md` decision 3's G8 row true. Verified over the COMPOSITION's compiled document (`audit_report`, the same walk `revl audit` renders); see the scope limit below for what the CLI does not do yet | `src/revl/boundary.py` (the walk), pinned for both forms by `tests/test_439_a2a_transport.py::test_the_a2a_crossing_is_on_the_g8_audit_surface` and `::test_all_four_task_crossings_are_on_the_g8_audit_surface` |
| F6, the trace funnel | every host-trace event, a crossing fault's included, passes the one choke point that removes registered secret values | `backends/python/runtime.py:1198` |
| F7, temporal residue | unchanged in kind: the crossing is an ordinary emission with no inverse, so it adds no residue class, and the withdrawal cascade is what settles it (slice T0) | `src/revl/run.py:1870` |
| G4, no inverse | every synthesized op is an `emission`; the provider emits no `undo`, and the four-op projection's `_cancel` is a `compensate` of `_start`, not an inverse | `src/revl/synthesize.py:839`, `src/revl/a2a_task.py:62` |
| G9, sink refusal | the `Untrusted[T]` return, above, read as a taint source with origin `net` | `src/revl/taint.py:339` |
| the failure settlement | `on_failure(withdraw)` is the default and raises a marker-bearing fault the runtime turns into provider withdrawal; `on_failure(result)` brings the failure back in band and does not withdraw | `src/revl/synthesize.py:654`, `src/revl/run.py:1870` |
| version honesty | question (3) | `src/revl/import_a2a.py:185`, `:317` |
| the operator halt | our side deschedules; the peer's Task keeps running, and the row must not claim otherwise | `docs/design/439-a2a-task-lifecycle.md`, decision 6 |

G2, G5 and G7 are unchanged and are stated with their A2A shape in
`docs/design/439-a2a-task-lifecycle.md`, decision 3; this table does not restate
them, and neither note contradicts the other.

## Scope limits recorded honestly

- **Root-endpoint only.** A remote row carries a bare authority (`check_address`
  refuses a path or userinfo), so `through a2a` POSTs JSON-RPC to the authority's
  HTTPS root. An agent served under a path is the importer's case, where the full
  `url` is read from the Agent Card. The header records that the peer authority
  is the endpoint root.
- **`revl audit <file>` does not resolve a composition.** The synthesized
  provider exists only inside the COMPOSITION document, and the CLI's audit path
  compiles its arguments as MODULES (`src/revl/__main__.py`, `compile_files`), so
  pointing it at a composition document prints an empty surface rather than the
  remote row's crossings. The G8 property itself holds and is pinned over the
  composition document, as the guarantee table says. Closing the CLI gap is a
  `__main__` change and is named here so the table's claim is not read as more
  than it is.
- **`@py` tier only.** As with the canonical wire, an `emission` method emits a
  synchronous ts function and a network round trip is not synchronous, so a ts
  body would be `await` inside a non-`async` function. The remote row must not
  recolour a `service` it did not write (unlike the importer, which writes it),
  so the ts projection waits on the async crossing.
- **Value tainting (slice C3) has landed, shared with the canonical wire.** Item
  424 D-424c.9 requires every value a remote provider returns to be
  `Untrusted[T]`. This is now done for BOTH wires together, in the one place they
  share (`_remote_source`): the synthesized extern's declared return is wrapped
  `Untrusted[<T>]` (`on_failure(result)` wraps the whole `Result[T, Str]`), which
  `taint.extract_and_normalize` reads as a taint source whose origin is the
  crossing's reach class (`net`). The provide method returns that source, so the
  flow walk taints it interprocedurally at every consumer of the key — exactly
  how `revl import a2a` taints by declaring its service operation
  `-> Untrusted[Str]`, except here the service the engineer wrote is unchanged
  and the qualifier lives only on the synthesized crossing. A remote result
  reaching a `Trusted[T]` sink is refused (G9) with no `endorse`, and admits with
  one. The taint IS the admission fact of remoteness: a consumer of a LOCAL
  provider of the same service is untainted and unchanged (D-424c.1), so bringing
  the provider back in-process removes the qualifier with no source edit. Because
  the qualifier is orthogonal to the base type and stripped before base typing,
  the synthesized provider still satisfies the service's declared `-> T`.

## What is NOT decided, and why

Each open item names what a future implementation PR must show to close it, and
what it must NOT show. An item with no exit test would be a wish, not an open
question.

1. **The `Stream[T]` sugar over the four ops (slice T2).** NOT decided, and
   deliberately blocked: a `Stream[T]` as a service operation's return is item
   130's surface and item 130 has not admitted it. What IS decided is the shape
   underneath (question (1)), so T2 is a projection change rather than a semantic
   one. Exit test: the four ops reachable as one stream-shaped operation, every
   element `Untrusted[T]`, with the teardown the handle form already has.
   Negative exit test: a stream element that is not `Untrusted`, or a teardown
   that leaves the peer's Task running with no local record of it.
2. **Push notifications (slice T3).** NOT decided, because an inbound callback is
   a PROVISION, not a client call, so it lands through item 457 slice S1's routed
   endpoint rather than through this binding
   (`docs/design/439-a2a-task-lifecycle.md`, decision 5). Exit test: a routed
   endpoint that receives a peer's notification and wakes the waiting operation,
   with the payload `Untrusted` and the endpoint's reach declared. Negative exit
   test: a notification path that admits, or re-admits, anything about the sender.
3. **`tasks/resubscribe` and `tasks/get` after our own crash.** NOT decided, and
   the missing piece is not the HTTP call: it is a task id that survives us.
   `TaskRef` is a value the consumer threads through `poll` / `reply` / `cancel`
   (`src/revl/a2a_task.py:67`), and this note does not decide whether it should be
   recorded durably, nor how. A durable id needs provider-declared replay (item
   130's territory) before it needs a call. Exit test: a run that crashes mid-task
   and, on recovery, resumes or cancels the SAME task id with its terminal state
   observed exactly once. Negative exit test: recovery that starts a SECOND task
   for the same crossing, or that reports a terminal state it never observed.
4. **The rest of the `Part` surface.** A `FilePart` with inline bytes LANDED. A
   `DataPart` is refused because arbitrary structured JSON needs the tagged half
   of the canonical encoding, which is item 424 slice C1's to build; a file reply
   that carries only a `uri` is a fault today, because following it would be a
   SECOND crossing with a reach of its own (`src/revl/synthesize.py:702`). NOT
   decided: whether that second crossing is ever worth a reach, and what a
   `DataPart`'s schema would be checked against. Exit test for either: the new
   shape is admitted only with a declared reach (the fetch) or a declared encoding
   (the `DataPart`), and every value it produces is `Untrusted`. Negative exit
   test: a `uri` reply followed silently, or a `DataPart` flattened onto a text
   `Part` without saying so.
5. **A file `Part` on the coloured `@ts` tier.** NOT decided, and blocked on the
   async crossing: an `emission` method emits a SYNCHRONOUS ts function, the row
   cannot recolour a service it did not write, and a `Uint8Array`/base64 binding
   would still be `await` inside a non-`async` function. Exit test: the ts body
   emitted under the `tsc --strict` gate, with the crossing's deadline. Negative
   exit test: a ts body emitted with a synchronous signature it cannot typecheck.
6. **gRPC.** NOT decided and REFUSED under any label on both entry points: it is a
   binary transport over HTTP/2 with protobuf framing, not the JSON POST either
   synthesizer emits, so it cannot ship honestly as `a2a`
   (`src/revl/synthesize.py:1568` names it in the refusal). Exit test: a real gRPC
   binding with its own `through` name, its own header and its own note. Negative
   exit test: gRPC shipped as a sub-transport of `a2a`, or a JSON body sent to a
   gRPC endpoint under an `a2a` claim.
7. **The failure-channel funnel (question (2), the fourth layer).** DECIDED and
   LANDED as slice B1, on the py tier, on all three generated bodies. Its exit
   test is
   `tests/test_439_a2a_transport.py::test_a_peer_cannot_echo_the_callers_argument_onto_our_error_channel`
   (a peer-supplied `error.code` echoing the caller's own argument text is
   scrubbed, and the sentence survives) and its negative exit test is
   `::test_the_funnel_matches_exactly_and_never_by_pattern` (a peer string that
   merely resembles an argument is left verbatim, because the match is exact).
   `tests/test_import_a2a.py::test_the_peers_error_code_cannot_echo_the_callers_argument`
   is the same pair on the importer's body.
   Still open, and named so it is not mistaken for done: the **`@ts` half of the
   funnel**. The importer's coloured ts body carries B1's correlation identity
   and its three envelope gates, but not the argument scrub. Exit test: the ts
   body's thrown error scrubbed the way the py one is, under the
   `tsc --strict` gate, with the placeholder equal to `bridge.ts`'s
   `REDACTED_ARG`. Negative exit test: a ts scrub by pattern, or one that loses
   the error's shape.

Item 439 is NOT closed by this note. Items 1 and 3 are what stand between the
binding as landed and the protocol as specified.

## Files

- `docs/design/439-a2a-transport-binding.md` (this note): the binding, the
  answers to item 439's three questions, the per-guarantee table, and the open
  items above.
- `docs/design/439-a2a-task-lifecycle.md`: the decision of record for the Task
  lifecycle (decision 1), the runtime half of `on_failure(withdraw)` (decision 4)
  and the slices T0..T3. This note does not restate or contradict it.
- `src/revl/synthesize.py`: `BOUND_TRANSPORTS`, `check_transport`,
  `_check_a2a_method`, `_py_body_a2a`, `_a2a_header_lines`, `_check_long_running`,
  `_task_ops`, and the `is_a2a` branch in `_remote_source`. The C3 tainting is the
  `Untrusted[<T>]` wrapper on the synthesized extern's return in `_remote_source`
  (both wires) and the taint paragraph in `_remote_header`.
- `src/revl/import_a2a.py` and `src/revl/a2a_task.py`: the two entry points' shared
  constants (`A2A_VERSION`, the terminal-state list, the four-op vocabulary) and
  the version check at admission.
- `src/revl/a2a_boundary.py`: slice B1. The correlation identity, the three
  envelope gates, the result and task-identity shape gates, and the F5 funnel,
  all as EMITTED source, in the one place the three generated py bodies share
  them. Also the ts correlation binding and gates the importer's coloured body
  uses.
- `tests/test_439_a2a_boundary.py`: B1's shared contract. The placeholder and the
  needle bound pinned equal to `backends/python/confidential.py`'s, the gates
  present on every generated py crossing, the REST wire's one-way identity, the
  correlation refusal rendering nothing of the peer's, and the ts body's
  identity.
- `tests/test_439_a2a_transport.py`: the seam/remote-provider exit test for the
  binding, the C3 taint section, the modality refusals, the four-op projection,
  and `test_no_marked_value_can_cross_the_a2a_wire`, which pins question (2)'s
  precondition.
- `tests/test_439_a2a_task.py`: slice T0's exit test (the runtime maps a
  marker-bearing transport fault to provider withdrawal) and the T1 vocabulary.
- `tests/test_424_remote_row.py`: `test_a_named_through_transport_is_refused`
  updated, `a2a` moved from the refused set to the bound set; the C3 taint section
  for the canonical wire (a remote result is refused at a sink; a local provider of
  the same service is untainted).

One stale docstring this note does NOT fix, because it belongs to another lane:
`check_transport`'s own docstring (`src/revl/synthesize.py:1590`) still calls item
439's Task-lifecycle question "load-bearing open", which is true of the DEFAULT
single-crossing form it is describing and stale as a statement about the binding
as a whole (the four-op `long_running` form answers it). `src/revl/**` is out of
this note's lane.

Gate digest inputs (`tools/build_gate_crate.py` `DIGEST_INPUTS`,
`tools/build_gate_wasm.py`) are `selfhost/*.rvl`, `backends/rust/emit.py`,
`src/revl/lexer.py`, `src/revl/typecheck.py` and the build scripts. This slice
touches none of them (`through` and `a2a` are contextual keywords, not lexer
keywords), so no crate regeneration is needed and no drift gate reddens.
