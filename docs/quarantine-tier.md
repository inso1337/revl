# The quarantine tier — candidates prove themselves in the sandbox

**Status:** v1 implemented — `revl.mcp.quarantine` (`src/revl/mcp/quarantine.py`),
the `revl_quarantine` MCP verb and the swap admission hook in
`src/revl/mcp/server.py`, the `revl quarantine` CLI in `src/revl/__main__.py`,
the `quarantine required` policy directive in `src/revl/policy.py`, and
`tests/test_quarantine.py`. The substrate battery needs the standard wasm
toolchain (`wasm-tools` + `wasmtime`, the same the canonical ABI slice
established); it skips cleanly with a reason when they are absent, while the
flow logic is tested everywhere.

> The gauntlet proves a candidate *runs correctly*; the quarantine tier proves
> it *cannot escape while doing so* — physically, in the sandbox, before it
> touches a hosted tier.

## Why a quarantine tier

Item 24's threat model is deliberately honest that **"the gate does not sandbox
host code"** is a *non-goal*. Admission (`revl_admit`) proves a candidate is
structurally compatible (§5, G2/G3, no interface drift) and that its teardown is
derived (LIFO) — but all of that is *before anything runs*. The instant a
candidate's own body executes on a hosted tier, revl is trusting host code it
cannot look inside. A malicious or merely buggy candidate that faults there is
an **incident**: it already ran in the host.

The quarantine tier is the architectural answer, and it rests on the wasm tier's
defining property:

> **The paradigm is enforced by the sandbox — confinement is physical.**

A candidate compiled to a standard WASI-Preview-2 component and run under
wasmtime's component model cannot reach past its own linear memory. An
out-of-bounds access, an integer overflow, a divide-by-zero — all of these are
**wasm traps** the runtime catches. So we run the untrusted candidate *there
first*: an escape attempt becomes a trap the operator never has to clean up
after, instead of an incident in the host.

## The flow: the gauntlet, staged through the wasm substrate

Quarantine is **"run the gauntlet (item 31), but inside the wasm sandbox."** It
reuses the gauntlet verbatim, then adds the substrate battery:

1. **Grade with the gauntlet.** The candidate is graded exactly as `revl_gauntlet`
   grades it — admission *proved*, teardown *derived*, the G8 boundary
   *enumerated*, and the host no-residue lifecycle *tested* in an isolated
   scratch session. A candidate admission refuses never reaches the substrate
   (`verdict: rejected`).

2. **Lower to a standard component.** An admissible candidate is compiled to a
   real WASI-P2 component over the landed canonical ABI
   (`backends/wasm/canonical.py`, slice 41-3), imported **read-only** — a
   parallel session owns that file. `wasm-tools` wraps the core module into a
   component and `wasm-tools validate --features all` proves it is a valid one.

3. **Run the lifecycle + fault battery on the substrate.** Every Str-surface
   boundary function is booted and invoked under wasmtime's **component model**
   (`wasmtime run --invoke`, which only accepts a component) across a probe
   battery:

   | probe | exercises |
   |---|---|
   | `""` (empty) | the canonical string ABI edge — a valid pointer, length 0 |
   | `"a"`, `"revl"` | nominal round trips (lift a bare `(ptr,len)` in, lower one out) |
   | `"x"×64` | the guest allocator (`cabi_realloc`) on a larger string |

   A probe that **returns** is a clean round trip. A probe that **traps** is
   caught here — wasmtime exits non-zero, the host was never touched — and
   recorded with the runtime's own failure detail. It is never re-raised.

### Verdicts

| verdict | meaning | admission |
|---|---|---|
| `passed` | admissible, and every substrate probe returned cleanly | eligible |
| `trapped` | admissible, but a probe **trapped in the sandbox** — contained, host untouched | **not** eligible |
| `rejected` | admission refused; the candidate never reached the substrate | — |
| `deferred` | no Str-surface boundary function (the aggregate follow-on) | — |
| `unavailable` | `wasm-tools`/`wasmtime` absent; grade + lowering still ran | — |

A `trapped` verdict is the tier's whole point: the candidate's fault would have
*escaped on a hosted tier*, but in quarantine it is a caught trap, **proven by
an actual wasmtime run, not asserted.** The exit test drives a real
integer-overflow candidate (`Int` is i64 and overflow traps — see
`docs/arithmetic.md`) whose trap the sandbox physically contains.

## The admission gate

> Admit only after quarantine passes — *where policy says so.*

This reuses the two authorities the composition already has:

- **Item 33 boundary policy** decides *whether quarantine is required*. A policy
  bound to the session (`session.sandbox`) that declares `quarantine required`
  (DSL) or `{"quarantine": {"required": true}}` (JSON) makes admission
  conditional on a `passed` verdict.
- **Item 55 operator authority** decides *who may bypass* a required quarantine
  that did not pass. An operator whose profile grants
  `quarantine-bypass` over the candidate's subjects (e.g.
  `operator root may quarantine-bypass on *`) may admit an un-quarantined
  candidate; the admission is stamped as a bypass.

`quarantine.admission_decision` returns the decision; `quarantine.gate_swap`
enforces it inside `revl_swap`. **With no requiring policy, the gate is inert
and the default swap path pays nothing** — quarantine is opt-in, exactly like
the item-33 sandbox and item-61 leases it sits beside.

```
G4 (per component)        what may a component reach?
policy item 33 (compose)  what may anything in the composition reach?
operators item 55         what may the operator driving the session do?
quarantine item 45 (this) may an untrusted candidate run on a hosted tier
                          at all — did it survive the sandbox first?
```

## Using it

MCP verb (read-only — the live composition is never touched):

```json
{"name": "revl_quarantine", "arguments": {"source": "fn tag(s: Str) -> Str { return `[${s}]` }"}}
```

CLI:

```
revl quarantine candidate.rvl              # verdict + substrate counts
revl quarantine candidate.rvl --json       # the full report
revl quarantine candidate.rvl --policy quarantine.txt   # + admission decision
```

Exit status: `0` when the candidate passed (or was deferred/unavailable —
nothing to fail on), `1` when it was trapped or rejected, and — with
`--require-runtime` — `3` when the toolchain is absent so the candidate could
not actually be proven.

## Scope: Str-surface this slice, aggregates next

The canonical ABI slice that landed (41-3) presents the pure functions whose
**whole signature is `Str`** — one `Str`-taking, `Str`-returning function is the
minimal standard component a host loads and runs. So this tier targets the
**Str-surface candidate**: a common untrusted-transform shape (sanitizers,
formatters, templaters, redactors).

A candidate that needs **records / lists / variants across the quarantine
boundary** cannot yet be lowered — those types lift/lower at the canonical
boundary in a **follow-on slice**, gated on the parallel aggregate canonical
work. Such a candidate is reported `deferred` with a reason that names the
follow-on. It is **refused honestly, never faked** through a boundary that
cannot carry it — the same discipline the canonical emitter itself uses when it
leaves a non-`Str` function off the component interface.
