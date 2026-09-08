# 439: the A2A Task lifecycle binding, and the first slice

**Roadmap:** item 439 · **Issue:** #118 · **Reasoning of record for the landed slices:** docs/design/439-a2a-transport-binding.md (the importer, `through a2a` / `through a2a_rest`, terminal-only `message/send`, `FilePart`, `Untrusted[T]` tainting) · **Status:** DECISION, 2026-09-08

## What this note decides

439-a2a-transport-binding.md left six things open and named the first as
load-bearing. Each is decided here:

1. The Task lifecycle: what a long-running, streaming A2A Task is in revl terms.
2. gRPC: whether the binding needs it.
3. The boundary and reach guarantees across the wire, stated once for both the
   terminal and the long-running forms.
4. The runtime half of `on_failure(withdraw)`.
5. Push notifications.
6. The remaining `Part` modalities and the ts tier.

Plus one interaction the batch made visible: what an operator E-Stop does to a
task that is running on a peer.

## Decision 1: a Task is stream-shaped, never session-shaped

A2A 1.0.0 gives a Task a lifecycle (`submitted`, `working`, `input-required`,
`auth-required`, then a terminal `completed` / `failed` / `canceled` /
`rejected`), a stream of status and artifact events (`message/stream` over
SSE, `tasks/resubscribe`), a poll (`tasks/get`) and a cancel (`tasks/cancel`).
The roadmap asked whether that maps to one emission, a stream (130) or a
session (250).

**It maps onto the three entry kinds revl already has, and the event feed is a
stream.**

| A2A | revl | entry kind |
|---|---|---|
| `message/send` reaching a terminal state in one round trip | one emission, the landed subset | emission, no inverse (G4 declared irreversible) |
| `message/send` that returns a non-terminal Task | one emission that STARTS the task and returns a `TaskRef` | emission with a `compensate`: `tasks/cancel` (247, audit-grade, best-effort) |
| `message/stream`, `tasks/resubscribe`, `tasks/get` | observations of a locally held listener: SSE connection or poll loop | a `bracket` whose inverse is the LOCAL close (drop the connection, stop the loop): host-local, infallible, non-emitting (G5) |
| `tasks/cancel` | the compensation of the start emission, and an explicit op the consumer may call | compensation (never a bracket inverse: it is remote and fallible) |
| `input-required`, `auth-required` | events the consumer sees; the answer is a second `message/send` carrying `taskId` | a second emission |
| a terminal state | the terminal event of the feed (`Done`, `Failed`, `Canceled`, `Rejected`) | the stream closes |
| peer gone: SSE EOF, poll deadline (`CROSSING_TIMEOUT`), transport error | an adapter-synthesised `Faulted` terminal | 130's rule 3.6 (provider death is a terminal, never silence) satisfied AT THE ADAPTER, because the peer is a claim and cannot be made to promise it |

Why not a session (250): a session is our own accumulator's timeline, with
rewind, fork and hash-bound commit, and its whole honesty model is about which
of OUR inverses may run. A remote task cannot be rewound, only cancelled;
binding it to a session would claim reversibility the peer never offered, and
would bind standing approvals (246 invariant 5) to a thing that is not ours.
Why not "one emission" alone: it is the landed subset and stays right for it,
but a long-running task has state changes the composition must observe, and an
emission observes nothing after it returns.

Why a stream fits exactly (docs/design/130-stream-reactive-types.md): a
subscription IS a bracket whose close is the cancellation path, single consumer,
bounded buffer with `error` as the default policy, closed by the owner's
teardown (G7) and by a handler failure (A8). Every one of those is what a task
feed needs, and none of it is new machinery.

## The surface, in two steps

Streams exist today only as the local `Stream.source()` provider; a required or
returned `Stream[T]` on a service operation is 130's own later slice. So the
binding lands in two steps, and the first needs no stream at all.

### T1: the explicit handle form (buildable now)

`stdlib/a2a.rvl` (pure revl, every tier):

```revl
pub type TaskRef   = { id: Str, context: Opt[Str] }
pub type TaskState = Submitted | Working | InputRequired | AuthRequired
                   | Completed | Failed | Canceled | Rejected | Unknown
pub type TaskEvent = Status(TaskState) | Artifact(Str) | Message(Str)
                   | Done(Str) | Faulted(Str)
pub fn is_terminal(s: TaskState) -> Bool
```

On `through a2a` / `through a2a_rest` and on `revl import a2a`, a skill whose
card declares `capabilities.streaming`, or any skill the composing engineer
marks long-running, projects four operations instead of one:

```revl
service Researcher {
  emission fn research_start(message: Str) -> Result[TaskRef, Str]
      compensate research_cancel(task)
  emission fn research_poll(task: TaskRef) -> Result[TaskEvent, Str]      // tasks/get
  emission fn research_reply(task: TaskRef, message: Str) -> Result[TaskEvent, Str]
                                                                          // message/send + taskId
  emission fn research_cancel(task: TaskRef) -> Result[Unit, Str]         // tasks/cancel
}
```

Every operation is a single crossing, so everything the landed wire decides
(one crossing, redirect refusal, the deadline, `on_failure`, the version claim,
the `Untrusted[T]` return) applies unchanged. `research_poll` returns one event
per call: the consumer drives the loop, and the adapter turns a deadline or a
transport failure into `Faulted`. The terminal-only form stays available for
skills that are not long-running, so nothing landed changes shape.

The `compensate` on `_start` is the one new thing the synthesizer emits, and it
is item 247's clause exactly as declared in DESIGN.md §3.5: on the owner's
abort the runtime issues `tasks/cancel` best-effort, records it as
`compensation-residue` if it does not land, and never pretends the task is
undone.

### T2: the stream form (when 130 admits a stream-valued service operation)

```revl
service Researcher {
  emission fn research(message: Str) -> Stream[Untrusted[TaskEvent]]
}

component Analyst requires researcher: Researcher {
  let sub = subscribe researcher.research("survey the field") undo sub.close()
  every ev in sub { ... }
}
```

`subscribe` opens the local listener (`message/stream` when the card claims
streaming, else the poll loop) and registers the bracket; `close` drops it,
host-local. The start emission and its `tasks/cancel` compensation are
registered by the subscribe step in that order, so the owner's LIFO teardown
closes the listener first and then, on abort, compensates. The element type is
`Untrusted[TaskEvent]`: every value that crossed is tainted (D-424c.9), and 249
slice B's propagation through `every` bodies is what the consumer's sinks see.
Single consumer, `error` backpressure by default, wasm refuses (130 §4.6).

Precondition, stated so it is not discovered: 130 must admit `Stream[T]` as a
service operation's return and as a required capability. Until it does, T1 is
the surface and T2 is sugar over the same four crossings.

## Decision 2: no gRPC, and not as a sub-transport

Refused under any label, on both entry points, as today. Reasons: A2A 1.0.0's
two JSON-body transports carry every method the binding needs, including
streaming (SSE) and polling; gRPC needs protobuf codegen per tier and a binary
framing the canonical encoding does not describe; and nothing the guarantees
below rest on is stronger over gRPC. The precondition to reopen is a real peer
in the harness workload that exposes gRPC only. If that arrives, it is a
separate transport with its own binding note, not `through a2a` wearing a
second wire.

## Decision 3: the guarantees across the wire, stated once

| guarantee | at the A2A boundary |
|---|---|
| G1 declared access | the consumer writes `requires researcher: Researcher` and nothing else; the synthesized provider's reach is `net.<host>` folded from the host alone (D-424c.10), never port or userinfo |
| G2 | one row per `(key, realm)`; two peers of one service are two realms |
| G4 | every synthesized op is `emission`; no inverse is synthesized (a peer's claim to have undone something is not a witness); cancel is a `compensate` |
| G5 | the only bracket is the local listener, whose close is host-local and infallible |
| G7 | intact: the stream bracket and the compensation are registered entries; the emissions are not, and there is nothing for G7 to miss |
| G8 | the boundary surface is the four (or one) synthesized externs, enumerable by `revl audit` |
| G9 | every returned value and every stream element is `Untrusted[T]` with origin `net`; a flow into a sink is refused without `endorse` |
| failure | `on_failure(withdraw)` by default: a transport fault on any of the four crossings, or a `Faulted` terminal on the feed, withdraws the provider (R2/R3); `on_failure(result)` keeps it wired and returns the `Err` |
| trust | the card and every reply are claims; no re-admission (337), no badge (D-424c.8); every A2A provider is item 329's untrusted-author case |
| version | "A2A 1.0.0 over JSON-RPC 2.0" or "over HTTP+JSON", exact, never bare "A2A" |
| operator halt | see Decision 6 |

## Decision 4: the runtime half of `on_failure(withdraw)`

The declaration, the check, the IR contract and the fault-raising body are
landed; the fault unwinds the calling fiber but does not withdraw the provision.
Decided: **the activation runtime maps a declared `TransportFault` raised by a
synthesized provider's method to provider withdrawal**, the second of the two
options 439-a2a-transport-binding.md named. The first option (joining the
placement bridge's `watch(on_lost)` monitor) is rejected because a `urllib`
body is not a seam client and a fake monitor connection would be a second
mechanism pretending to be the first. The fault carries the row label and the
crossing, the withdrawal cascades through R2/R3 exactly as peer death does, and
the consumer's teardown proof prints. py first; every other tier's activation
runtime adds the same catch at the same seam. This is slice T0 and both the
terminal and the task forms need it.

## Decision 5: push notifications are a routed endpoint

A push notification is inbound: the peer calls us. That is a provision, not a
client call, and revl now has the right primitive for it: an endpoint (docs/
design/457-endpoint-one-definition.md). A `route post "/a2a/notify/{task}"`
operation with a `Bearer` parameter carrying the push token, validated
explicitly by the handler against the token the start call configured, feeds
the same `TaskEvent` shape into the consumer. Deferred behind 457 S1. Polling
stays the canonical feed; push is an optimisation that must not change what the
consumer sees.

## Decision 6: an E-Stop leaves the remote task running, and says so

A halted composition (docs/design/443-estop-tier-contract.md) runs no
compensation, so the `tasks/cancel` registered on a `_start` is stranded. The
`estop-stranded` record names the task id in its captured args, and the
compensation is keyed by that id, so `revl recover` may re-issue `tasks/cancel`
under item 309's keyed rule; an unkeyed peer that answers `canceled` twice
differently is the operator's. Our halt never halts the peer, and the report
does not pretend it did: the stranded line is the operator's list of tasks that
are still working somewhere else.

## Deferred, with preconditions

- **`DataPart`** (structured JSON): needs the tagged half of the canonical
  encoding reachable from a synthesized host body; that is `revl export
  client`'s projection (C1) made available to the synthesizer. Refused, never
  flattened, until then.
- **The ts tier** for the remote row: an `emission` method emits a synchronous
  function and a network round trip is not synchronous; waits on the async
  crossing recolouring, as the canonical wire does.
- **A file `Part` by `uri`**: a second crossing to a second host is a second
  reach; refused until a reach bound for it is declared on the row.
- **`tasks/resubscribe` after our own crash**: the listener is
  `unreconstructible` residue (130 §4.9) unless the task id is durable; T2 may
  declare `replay(from: task_id)` once 130 lands provider-declared replay.

## Slices and exit tests

| slice | delivers | tier | depends on | exit test |
|---|---|---|---|---|
| **T0** | `TransportFault` to withdrawal in the activation runtime | py | none | a fake peer that stops answering withdraws the synthesized provider; the consumer deactivates reactively with a no-residue proof; under `on_failure(result)` the same fault is the `Err` and the provider stays wired |
| **T1** | `stdlib/a2a.rvl`; the four-op projection on both entry points; `compensate` emitted for `_start`; `Faulted` synthesised on deadline; `Untrusted` on every return | py | T0 | against the fake A2A server used by `tests/test_439_a2a_transport.py`: `_start` returns a `TaskRef`; `_poll` walks `Working` to `Done`; a peer that answers `input-required` yields the event and `_reply` advances it; an owner abort issues exactly one `tasks/cancel` and records it; a peer that stops answering yields `Faulted` and withdraws; a `Done` payload reaching a shell sink is refused without `endorse` |
| **T2** | the stream form over the same crossings; SSE listener where the card claims streaming | py | 130's stream-valued service operation; T1 | `every ev in sub` sees the same event sequence T1's poll saw; unload closes the listener before the start's compensation runs (LIFO); a silently dead peer resolves the parked `next` as `Faulted` (130 exit tests 4 and 5 against a remote) |
| **T3** | push notifications as a routed endpoint | py, then ts | 457 S1; T1 | a pushed event and a polled event produce byte-identical `TaskEvent`s; a push without the configured token is `401` and feeds nothing |

T0 and T1 are the first implementable slice for #118. They close the
load-bearing question with a surface that exists on today's language, keep
every landed decision, and leave T2 as sugar whose only precondition is
external to this item.
