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
- The swapped component's process must **not be a network provider**. A process
  that declares `[processes.<p>].address` serves its seam over TCP + mutual TLS
  (`docs/network-placement.md`), and that address is part of the placement
  contract consumers on other machines hold — an item-56 network consumer in
  another process, or an item-151 `[remotes]` consumer in a wholly separate
  composition. The conductor cannot enumerate those consumers, let alone
  re-point them, and the successor is booted on a fresh local Unix socket, so a
  swap here would cut every remote caller off the seam while reporting success.
  It is refused with a diagnostic naming the process and its endpoint. To
  re-tier a network provider, bring the composition down and re-place it with
  `[processes.<p>] backend = "<tier>"`, which keeps the address (and its TLS
  material) declared in one place. Serving the successor on the *same* endpoint
  is a real feature — an address handover, with the cert material carried across
  and a story for the window where both processes bind the port — not a gap this
  pass papers over with a fresh socket.
- The swap is driven as a **command inside the running `revl run --placement`
  session** (a `swap>` prompt interactively, or a script on stdin — see
  "Scripted swaps" below), not a separate `revl swap` OS process. Re-pointing a
  live proxy mutates in-memory client state inside the already-running consumer
  process; the conductor already owns every child's control channel (its stdin),
  so the operation lives where that ownership lives. A fresh CLI process would
  have no handle on the running seam and would need a whole separate control
  plane to reach it.

## Scripted (non-interactive) swaps

The `swap>` prompt is for a human at a terminal. The same commands can be driven
from a **script**: when `revl run --placement` finds its stdin is not a tty, it
reads the swap script from stdin — one `swap <component> --to <backend>`,
`:keys`, or `:q` per line — through the same dispatch the prompt uses, and EOF
(a closed pipe, `< /dev/null`, or the last line) tears the placement down. That
is the same stdin-closed contract single-process `revl run` already follows, and
it is what lets the live migration run as a repeatable exit test rather than only
a hand-driven demo. Nothing about the swap itself changes: admission, re-point,
drain, and the no-residue proof are identical to the interactive path.

    printf 'swap MemCache --to py\n:q\n' \
      | revl run demo/live_systems/app.rvl --placement demo/live_systems/split.toml

## Running the live-systems demo (v3.0 gate E3)

The live-systems story — cross-tier live migration (`swap`), the runtime causal
oracle (`revl why`, [why-runtime.md](why-runtime.md)), and plan/apply with a
derived rollback (`revl apply`, [apply.md](apply.md)) — runs end to end from a
clean checkout as one scripted command:

    # once, to install the cordis-py runtime the live stages need:
    sh backends/python/setup.sh

    # the demo (also available as `make demo`):
    backends/python/.venv/bin/python demo/live_systems/run_demo.py

It shells out to the real `revl` CLI for each stage and asserts the observable
outcome: the swap boots a successor on the target tier, re-points every consumer,
and drains the old provider with a no-residue proof; `revl why` names the
migration in the cause chain and its oracle reports the runtime tore the
composition down in exactly the compiler's predicted set and LIFO order; and
`revl apply` lands a planned change, then rolls a forced mid-plan failure back by
derived LIFO inverses with no residue. All run artifacts go to a throwaway temp
dir, so the demo depends on no developer-machine state and passes on a second
run as cleanly as the first. The composition it operates on lives beside the
runner in `demo/live_systems/` (`app.rvl`, `split.toml`, `candidate.rvl`). CI
runs the same gate in the conformance job; `pytest tests/test_e3_demo.py` runs
it wherever the cordis-py runtime is present and skips loudly where it is not.

This demo uses a **py-to-py** swap: both processes are py, so the whole live
migration exercises the boot / admit / re-point / drain / no-residue mechanism on
the cordis-py runtime alone (roadmap item 23 runs on py). Swapping to a different
tier (`--to node|rust|java|go`) is the same operation over the same seam; it
additionally needs that tier's toolchain, which the placement smoke covers.

## Deferred: shadow verification

The next layer on top of `swap` is **replay-based shadow verification**: record
the provided-service timeline against the old provider, re-drive it against the
candidate, and compare *before* cutover — so a candidate that would answer
differently is caught before any consumer sees it. The recorder that makes this
possible already exists ([replay.md](replay.md)); wiring it into the swap as a
pre-cutover gate is a separable follow-up. `swap` ships the cutover first.
