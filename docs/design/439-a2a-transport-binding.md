# 439: the A2A 1.0.0 transport binding for remote providers

Design note for roadmap item 439 (issue #118). This note records what the slice
in this branch builds and why, and what remains for full closure. It is not a
fresh design of the semantics: item 424 gap (c) already decided those, and item
439 in the roadmap already fixed the three load-bearing decisions. This binding
makes them concrete without reopening any of them.

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

Concretely, the mapping (`_py_body_a2a` in `src/revl/synthesize.py`):

| canonical seam envelope        | A2A 1.0.0 `message/send`                          |
| ------------------------------ | ------------------------------------------------- |
| `args[0]` (the one `Str`)      | `params.message.parts[0]` as `{kind:"text",text}` |
| `method` (the op name)         | `params.message.metadata["revl.skill"]`           |
| reply `value`                  | text of a TERMINAL `Task`/`Message` reply         |
| reply `{"ok":false,"error"}`   | a transport failure, a JSON-RPC error, a          |
|                                | non-terminal task, or a non-text reply            |

The row keeps everything the canonical wire already decides, because it reuses
the same machinery rather than reimplementing it: one crossing, redirect refusal
(`src/revl/crossing_redirect.py`), the `CROSSING_TIMEOUT` deadline, and the
`on_failure(withdraw|result)` branch. Only the payload built and the reply
parsed differ. The version claim and the terminal-state list are imported from
`import_a2a` (`A2A_VERSION`, `_TERMINAL_STATES`) so the two entry points onto the
protocol cannot drift.

## The three item-439 decisions, kept

1. **The Task lifecycle question is left open, not answered.** Item 439 asks
   whether an A2A Task maps to one emission, a stream (item 130), or a session
   (item 250), and calls it load-bearing. This slice binds only the subset where
   the question does not arise: a `message/send` whose task reaches a TERMINAL
   state (`completed`, `failed`, `canceled`, `rejected`) in that one crossing. A
   non-terminal reply (`working`, `input-required`, `auth-required`, `unknown`)
   is a fault at the boundary. The body never polls, never resumes, never
   guesses. This is the same subset `revl import a2a` binds, on purpose.

2. **The peer is a CLAIM, not a checked composition.** An external agent is not
   a revl composition, so nothing about it is verified. A remote row already
   admits, verifies and re-admits nothing about the callee (a client is the
   sender, D-424c.8; item 337 requires the receiver to re-compile from its own
   source). Over A2A that is true by construction, so every A2A provider is item
   329's untrusted-author case. The synthesized header states this in the remote
   row's own "no verified remote badge" language.

3. **The version claim is exact.** The header says "A2A 1.0.0 over JSON-RPC
   2.0", never bare "A2A". The protocol moves and a binding that followed it
   silently would assert a compatibility nobody checked.

## What `through a2a` refuses, and why

The binding holds the same honesty line the importer does. Each refusal names
what it refuses:

- **A non-`a2a` `through <name>`.** Still refused by `check_transport`. gRPC in
  particular is a binary HTTP/2 transport with protobuf framing, not the JSON
  POST this synthesizer emits, so it cannot ship under the `a2a` label or any
  other.
- **A method that is not text-in / text-out.** A2A `message/send` crosses one
  user message. `through a2a` therefore binds a method of shape
  `emission fn op(message: Str) -> Str` (or `-> Result[Str, Str]` under
  `on_failure(result)`). More than one parameter, a non-`Str` parameter, or a
  non-`Str`/`Result[Str,Str]` return is refused naming the method, rather than
  flattened onto the one text `Part` the crossing sends. This is the only
  modality an A2A boundary describes well enough to project; a `FilePart` or a
  `DataPart` is a later slice.
- **A non-terminal task.** See decision (1).

## Scope limits recorded honestly

- **Root-endpoint only.** A remote row carries a bare authority (`check_address`
  refuses a path or userinfo), so `through a2a` POSTs JSON-RPC to the authority's
  HTTPS root. An agent served under a path is the importer's case, where the full
  `url` is read from the Agent Card. The header records that the peer authority
  is the endpoint root.
- **`@py` tier only.** As with the canonical wire, an `emission` method emits a
  synchronous ts function and a network round trip is not synchronous, so a ts
  body would be `await` inside a non-`async` function. The remote row must not
  recolour a `service` it did not write (unlike the importer, which writes it),
  so the ts projection waits on the async crossing.
- **Value tainting is slice C3, shared with the canonical wire.** Item 424
  D-424c.9 requires every value a remote provider returns to be `Untrusted[T]`.
  That is slice C3, which neither the canonical wire nor `through a2a` has landed.
  Until it does, the header carries the same caveat the canonical row already
  carries: a value that crossed this boundary is indistinguishable at a call site
  from a local one, so treat it as untrusted. Both wires gain tainting together.

## What remains for full closure of item 439

1. The Task lifecycle (decision (1)): `message/stream`, `tasks/resubscribe`,
   `tasks/get` polling, and the mapping of a long-running Task onto an emission,
   a stream (item 130), or a session (item 250). This is the load-bearing open
   question and it is still open.
2. Push notifications and webhook delivery (an inbound callback is a provision,
   not a client call).
3. Non-text `Part`s (`FilePart`, `DataPart`) and richer skill schemas.
4. gRPC and HTTP+JSON/REST as `through a2a` sub-transports (the importer already
   binds JSON-RPC and HTTP+JSON; the row binds only JSON-RPC so far).
5. The `Untrusted[T]` return tainting, slice C3, shared with the canonical wire.
6. The runtime half of `on_failure(withdraw)`: wiring a transport fault into the
   provider-withdrawal cascade (R2/R3). The declaration and the fault are built;
   the cascade is armed by the placement bridge's monitor connection, which a
   synthesized row does not yet join (see `test_424_remote_row.py`).

## Files

- `src/revl/synthesize.py`: `BOUND_TRANSPORTS`, `check_transport`,
  `_check_a2a_method`, `_py_body_a2a`, `_a2a_header_lines`, and the `is_a2a`
  branch in `_remote_source`.
- `tests/test_439_a2a_transport.py`: the seam/remote-provider exit test for the
  binding.
- `tests/test_424_remote_row.py`: `test_a_named_through_transport_is_refused`
  updated, `a2a` moved from the refused set to the bound set.

Gate digest inputs (`tools/build_gate_crate.py` `DIGEST_INPUTS`,
`tools/build_gate_wasm.py`) are `selfhost/*.rvl`, `backends/rust/emit.py`,
`src/revl/lexer.py`, `src/revl/typecheck.py` and the build scripts. This slice
touches none of them (`through` and `a2a` are contextual keywords, not lexer
keywords), so no crate regeneration is needed and no drift gate reddens.
