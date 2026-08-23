# revl swap — verified live migration across tiers

**Status:** implemented (2026-08-23) · `revl swap <component> --to <backend>`
inside a running `revl run --placement` session · companion to
[interop-bridge.md](interop-bridge.md) and [service-compat.md](service-compat.md)

Placement (`revl run --placement`) brought the six runtime tiers into one
lifecycle: a component provided in one process, on one tier, consumed in
another, over the canonical wire encoding. That made tier membership something
you can change *while the composition runs*. `revl swap` is that operation:

    swap <component> --to <backend>

It moves one running component to another tier — re-hosting its provider in a
fresh process on the target backend — without stopping the composition and
without a gap in the service it provides. Consumers keep calling across the
cutover.

## What it does, in four steps

1. **Boot** the candidate provider in its own process on the target tier
   (`--to py|node|rust|java|go`), serving the swapped component's keys on a new
   socket. Same source, re-placed; the candidate already speaks the protocol
   because the wire encoding is canonical across tiers.
2. **Admit** it against the *running* manifest ([admission gate](service-compat.md)):
   the candidate is recompiled with `manifest=<running>, replacing=<component>`,
   so every running consumer's call site is re-checked against the candidate's
   interface, and the swapped service must be **transport-safe** (async,
   value-typed) — a tier swap re-points it across a process seam, and only a
   transport-safe service crosses cleanly (interop-bridge.md §4). If admission
   fails, the swap **refuses** with the guarantee-naming diagnostic and the
   running composition is untouched — nothing was re-pointed, no blip.
3. **Re-point** each consumer's proxy from the old provider's socket to the
   new one. This is the one genuinely new mechanism (below).
4. **Drain and tear down** the old provider, LIFO, and prove no residue: the
   old process disposes consumers-before-providers and prints a per-process
   residue proof (`[name] residue no residue | ...`) before it exits.

Swap back at any time by swapping the same component `--to` its original tier.

## The re-point: reconnect-to-successor, not withdraw

The per-proxy monitor thread (`backends/python/bridge.py`, `_Client.watch`)
already knew one thing: when the provider process dies, its idle monitor
connection hits EOF, and the proxy **withdraws** — disposes its own fiber, the
provision is withdrawn, dependents deactivate with ordered teardown (R2/R3).
That is the right behaviour for an *unplanned* death.

A swap is a *planned* cutover, and it must not read as a death.
`_Client.repoint(new_socket)` teaches the same client to carry the seam to a
**successor** instead:

- it dials the successor **before** taking any lock, so a successor that never
  came up raises there and leaves the live seam untouched (the swap refuses);
- it swaps the RPC and monitor connections over **under a lock** that any
  in-flight call already holds — so a call mid-flight at cutover **drains**
  against the old provider first, then subsequent calls go to the successor;
- it bumps a **generation** counter. Each monitor thread is bound to the
  generation it started on. When the old provider is then torn down, its
  monitor connection's EOF is recognised as the expected end of a *superseded*
  generation and does **not** fire the withdrawal. The current generation's
  monitor still withdraws on a real death — the unplanned path is unchanged.

So: unplanned death still withdraws; a planned cutover re-points. That
distinction is the whole point of the item.

## Drain semantics (v1)

In-flight calls at cutover are **drained against the old provider (v1)**: the
client lock serialises an in-flight call ahead of the re-point, so it completes
against the old provider before the switch. A **mid-stream handoff** — moving a
call that is already in flight onto the successor — is a later refinement, not
part of this pass.

## Scope (v1)

- The swapped component must be **alone in its process** (its process is the
  provider being replaced). A component sharing a process with others is
  refused with a diagnostic; split it into its own placement process first.
- The swap is driven as an **interactive command inside the running
  `revl run --placement` session** (a `swap>` prompt), not a separate
  `revl swap` OS process. Re-pointing a live proxy mutates in-memory client
  state inside the already-running consumer process; the conductor already owns
  every child's control channel (its stdin), so the operation lives where that
  ownership lives. A fresh CLI process would have no handle on the running
  seam and would need a whole separate control plane to reach it.

## Deferred: shadow verification

The next layer on top of `swap` is **replay-based shadow verification**: record
the provided-service timeline against the old provider, re-drive it against the
candidate, and compare *before* cutover — so a candidate that would answer
differently is caught before any consumer sees it. The recorder that makes this
possible already exists ([replay.md](replay.md)); wiring it into the swap as a
pre-cutover gate is a separable follow-up. `swap` ships the cutover first.
