# swap: verified live migration across tiers

**Status:** implemented (2026-08-23) · companion to
[interop-bridge.md](interop-bridge.md) and [service-compat.md](service-compat.md)

`swap` is **not a `revl` subcommand**. It is a verb of the REPL that
`revl run --placement` drops you into once every process is up:

```
revl run app.rvl --placement topology.toml
swap> swap <component> --to <backend>
swap> :keys
swap> :q
```

Typing `revl swap` at a shell is an invalid choice. The same verbs can be piped
in on stdin instead of typed, which is how the exit test drives it (see
"Scripted (non-interactive) swaps" below). Source: `swap_repl` in
`src/revl/placement.py`.

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

## What the successor's spec carries

The successor is a new placement process, so it gets a new spec rather than a
copy of the predecessor's. The rule for what goes in it:

> **A spec key that carries a security property must either be carried across
> the swap, or the swap must refuse.**

A swap is ordinary use, not an edge case. A property that holds at boot and
stops holding at the first swap is not a property, and the failure is silent:
the swap reports success either way. Both halves of the rule are in use today.

*Carried.* The successor's spec is key-for-key the per-process boot spec, built
for the one component the successor hosts:

- the **correlation guard** (`serve.correlation`), so the seam stays guarded
  after the cutover rather than admitting anybody. Carried only onto a tier
  that can run the guard, which is the same rule the boot path applies;
- the **host-module pins** (`refs`, `refRoot`, `stdlibRefRoot`), so the node
  runner's `@ts ref` deploy-contract hash check still runs before the
  successor imports any host code. The pins are scoped to the successor's own
  slice and read off the running composition's IR, which is the same document
  the successor's tier artifact is emitted from, so they describe the bytes the
  process is about to load;
- the **operator E-Stop latch** (`estopLatch`), so the button that armed the
  predecessor still halts the successor. A composition that were haltable only
  until its first swap would not be haltable: an operator would press the
  button, read a report, and still have a live process running. Carried only
  onto a tier that has an E-Stop seam, the same rule the boot path applies —
  a successor on a seamless tier is not silently un-haltable, it simply joins
  the population the conductor SIGKILLs outright and the halt report names as
  residue UNKNOWN, exactly as it would had the placement booted it there;
- the **§46 intra-process dependency edges** (`depends`), computed for the
  successor's component set;
- the **model provider configuration** (`providers`), when the predecessor
  served model keys in-process under `revl run --placement --providers`. The
  boot path builds no proxy for such a key, so without it nothing would serve
  the key after the cutover. Carried only onto the py tier, the one tier that
  binds a model host, the same rule the boot path applies.

*Refused instead.* Where carrying a key correctly is not possible, the swap
refuses up front and leaves the running composition untouched. A **sandboxed
component** is the standing example: moving it across an isolation boundary
changes the running system's security posture, so `revl swap` names the gap and
declines (item 411) rather than booting a successor whose sandbox envelope it
would have to invent.
A component whose process loads a **managed model role** (one revl loads and
unloads itself, docs/model-providers.md) is refused too. A provision belongs to
one process, so the successor would load the member and the predecessor's
teardown would then unload it from under the successor. Restart the placement
to move such a component.

One key is neither carried nor refused, and that is deliberate: `probe`. A
process runs the probes its own `[processes.<name>]` entry declares, and the
successor is a synthesized process the placement file does not name, so its
probe list is empty by the same rule the boot path applies. Probes are one-shot
boot smoke calls that invoke real service methods; re-firing the predecessor's
on a cutover would perform operator-authored side effects at a moment no
operator asked for, and the swap already carries its own verification (the
admission gate, the repoint acknowledgement, and the drain plus residue proof).

`tests/test_swap_ref_pins.py` holds the guard for the rule: it reads both spec
literals out of `src/revl/placement.py` and fails when a key exists in one and
not the other, so the next key added to the per-process spec cannot go missing
from the successor.

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

## Proving the boundary (in-process hot-swap)

Everything above is the placement swap, which moves a component to a fresh
process. The in-process hot-swap is a different path: `Session.swap`
(`src/revl/mcp/session.py`, the call under `revl_swap`, `revl_edit` and
rollback) disposes generation N and boots N+1 **in the same process**.
Disposal running does not prove that N left nothing behind, so the swap can be
asked to prove it (issue #2277). For now the option is Python API only: none
of the MCP verbs passes it yet.

```python
session.swap(ir, origin=origin, prove_boundary=True)
```

With the option, the swap seals the boundary after N's teardown and before
N+1 loads. It compares the process against the baseline the session took
before its first generation booted. If anything is off baseline, the swap
refuses by name, boots N again, and N keeps serving. A clean seal adds the
proof to the answer as `boundary`:

```json
{"noResidue": true,
 "checks": {"residue": true, "hostResources": true, "escrow": true,
            "registers": true, "threads": true, "tasks": true, "env": true,
            "modules": true},
 "detail": {...}}
```

A refusal is a `BoundaryRefused` (a `SessionError`). Its `code` is the first
refusal, `refusals` lists all of them, and `boundary` holds the proof that
refused:

| Refusal | What was off baseline |
|---|---|
| `RESIDUE_LEFT` | An R4 teardown check failed: the cordis registry, provisions, effect stack or listeners, an unresolved compensation, or a host `new`/`open` with no `drop`/`close` in the host trace. These are the same checks `unload` reports. |
| `BOUNDARY_UNVERIFIED` | The driver captured no host trace, so the host-resource pairing could not run. A check that did not run is never a pass. |
| `ESCROW_OPEN` | A witnessed mutation is still escrowed, or a deferred emission is still queued, on the session owner. It stays the session's to commit or abort. |
| `REGISTER_DRIFT` | A runtime register in `swap_boundary.REGISTERS` holds something it did not hold at the baseline, for example a trace observer that was never unsubscribed. |
| `THREAD_LEFT` | A thread is alive that was not alive at the baseline. |
| `TASK_LEFT` | An asyncio task is pending on the session's event loop that was not pending at the baseline. The loop outlives the swap, so the task would keep running under the successor's calls. |
| `ENV_DRIFT` | An `os.environ` key was added, removed or changed. The proof names keys, never values. |
| `MODULE_LEFT` | A `sys.modules` entry appeared that is not the generation namespace (`revl_run_gen*`), the standard library or revl's toolchain, or a name the baseline held now maps to a different module object (listed in `detail.modulesReplaced`). A `@py ref` module counts, because the successor could import N's copy. |
| `TIER_UNSUPPORTED` | The proof was asked for on a tier other than py. Other tiers boot each generation as a child process, so there is no shared boundary. This refuses before anything is torn down. |

Two runtime registers fill up for every generation and are never emptied by
the runtime: the config each component resolved (secret fields included) and
the decoding grammars each emitted module states. That residue is revl's, not
the author's, so with the option the swap withdraws the predecessor's own
entries from both before comparing and lists them in `detail.discharged` (keys
only). The predecessor's entries are the ones its IR names: its component names
for the config table, and `Service.method` for every method with a response
grammar. An entry under any other name, such as another session's, is never
touched, and neither is one still holding the object it held at the baseline (an
earlier session's leftover). The tables are keyed by name, so a second session
running a component of the same name shares its entry. If the seal refuses, the
withdrawn entries go back before the predecessor is booted again, so the tables
end where the swap found them. The successor registers its own. The realm-label
table is not compared at all: it
interns stateless `(realm, key)` identities that every generation shares on
purpose.

What the seal does **not** cover, so a clean proof is not read as more than it
is:

- **The clock.** The session clock is a ratchet and N+1 reads a later time than
  a fresh process would (`_CLOCK_ANCHOR` is deliberately not compared).
- **Randomness.** The state of Python's `random` module is not reset.
- **The filesystem.** A committed witnessed write is meant to outlive its
  generation. A write from a `@py` body that goes around a witnessed extern
  is not seen.
- **Attributes of already-imported modules.** Modules are compared by name
  and object. A host body that patches an attribute of a module the process
  had already imported (say `json.dumps`) leaves the same module object
  behind, and the patch outlives the generation unseen.
- **Runtime globals left out on purpose.** `swap_boundary.NOT_COMPARED` names
  every mutable global of the runtime the seal does not compare, each with its
  reason: the realm-label intern table, the clock anchor, the trace callback,
  the trace-entry counter, the type lookup, the digest nonce, and the
  ContextVars, whose values are per context and so never reach the context the
  seal reads. A test holds `REGISTERS` and `NOT_COMPARED` equal to the
  runtime's mutable globals, so a register added later fails it until it is
  placed in one of them.
- **Reused identities.** A register's entries are compared by object identity
  (`id()`), which CPython reuses after an object is collected. A new object at
  a collected baseline object's address, under the same key, reads as the
  baseline one. Threads are held as objects, so this does not apply to them.
- **Other activity in the process.** Threads, environment and modules are
  process-wide, so a second session or the host program changing them between
  the baseline and the seal also refuses. This errs on the side of refusing.

A swap without `prove_boundary` behaves exactly as before. This is the proof
obligation only. It does not compare test results between N and N+1 in one
process. Whether a same-process A/B run can ever count as evidence is still
open on issue #2277.

## Deferred: shadow verification

The next layer on top of `swap` is **replay-based shadow verification**: record
the provided-service timeline against the old provider, re-drive it against the
candidate, and compare *before* cutover — so a candidate that would answer
differently is caught before any consumer sees it. The recorder that makes this
possible already exists ([replay.md](replay.md)); wiring it into the swap as a
pre-cutover gate is a separable follow-up. `swap` ships the cutover first.
