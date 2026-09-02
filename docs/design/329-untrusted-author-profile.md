# 329 — the untrusted-author admission profile (and 330's admit crossing)

Roadmap items 329 (the profile) and 330 (the in-language admit+run crossing).
This note records what the first cut delivers, and what the fuller wasm-import
path — deliberately deferred — still needs.

## The problem (item 24 / G8)

The composition gate already refuses a turn that reaches an *undeclared* service
(G1) and holds an ungranted `requires` inert against a granted-only provider set
(R2). That is enough for capability composition. It BREAKS on host blocks: a
model-authored

```
extern pure fn exfil(t: Str) -> Str = @py { import os; ... }
```

ADMITS and RUNS arbitrary host code, because G8's boundary is "verbatim host
code — unchecked inside" (item 24: *the gate does not sandbox host code*). When
the AUTHOR of the source is untrusted — the lighthouse code-mode direction,
where a model emits an arbitrary revl program every turn — that is an injection
hole: the turn is composed against a granted tool surface, but nothing stops it
from declaring its own escape hatch.

## The delivered cut (cheapest-first)

`revl.admit_profile.AdmissionProfile` — a compile-time admission profile, a
**compile refusal**, not a runtime policy check:

- **`no_extern`** — the admitted source may declare no new `extern`/host-block.
  Scoped to the ROOT module (the source being admitted), not the pre-granted
  imported closure. Refused structurally on the parsed AST, before any host body
  is lowered or run. Closes G8 for the untrusted author: it may only COMPOSE
  pre-granted services, never declare its own host code.

- **`granted`** — an allowlist of the service NAMES the admitted program may
  reach. A service a component `requires` must be in the granted set, unless the
  requirement BINDS to the turn's own provision. Bounds an admitted turn to a
  subset of a running composition's ambient services instead of all of them
  (R2, tightened).

  The internal-provision exemption is resolved by provision KEY, the way the
  link phase resolves a `requires`, not by service name. Keying it on the name
  was a full bypass: a candidate declared one throwaway component providing the
  same service under an unused key, which made the service count as "internal",
  and its real `requires <live-key>: <Service>` was then never checked at all.
  At wiring time it bound to the ambient, host-backed provider that owns that
  key, so `granted` was decorative. A requirement is exempt only when every
  component it binds to is one of the candidate's own and provides that key with
  the same service. The check fails closed: an indeterminate binding (no
  manifest, no provider of the key, a dangling routed leg, a requiring component
  that takes no place in the static table) is not exempt and must be granted
  like any other reach.

Threaded through `compile_source`/`compile_files` (`profile=`). `None` = trusted
author, byte-identical to before.

`AdmissionProfile.untrusted_author(granted)` is the standard profile for a
model-authored per-turn source: `no_extern=True` plus the granted allowlist.

## The in-language crossing (item 330)

`Session.admit(source, granted)` applies the profile and, on admission, wires the
turn into the LIVE driver with the session owner active, so the turn's crossings
(and the granted providers' crossings it drives) register into the enclosing 245
frame — commit persists them, abort reverts them, residue-free. The refusal is a
verdict (`AdmitVerdict`), handed back as data — the repair signal — never raised
into the turn.

`stdlib/admit.rvl` + `revl.mcp.admit_bridge` expose this as a **classified
`emission` crossing** a running composition invokes in-language
(`admission.admit(source, granted)`). That is the whole of 330: the
admit-decision — the type judgment that IS the permission decision — is now a
declared, classified boundary on the G8 audit surface, not undeclared host py
reaching sideways into the compiler. Host code stays only as a classified extern
body (`host_admit`), per the workload's rule.

A turn admitted through the crossing while a call is driving the loop is QUEUED
and wired the moment that call returns — a turn is never wired mid-call, exactly
as `demo/evolve_bridge.propose` never swaps synchronously. The crossing is
additive-only: a turn that would replace a running component is refused (hot-swap
is a separate, operator-gated verb).

## DEFERRED — the wasm-import path (item 45's confinement)

The no-extern cut forbids host code for the untrusted author. The fuller answer
item 329 names is to *allow* a per-turn host body but make its confinement
**physical**: compile the turn to the wasm substrate (the item-45 quarantine
tier's canonical ABI, `backends/wasm/canonical.py`) with the granted tools wired
in as **host imports**, run it under wasmtime's component model, and let an
escape attempt be a **wasm trap** — caught by the runtime, the host never
touched — rather than a compile refusal.

Why deferred:

- The quarantine tier today is a GRADING battery for a candidate
  (`revl.mcp.quarantine`), not a per-turn admit-into-a-live-composition path. It
  boots the candidate standalone in a throwaway sandbox; it does not wire the
  granted tools of a *running* composition in as host imports and let the turn
  call them.
- Pure confinement also SEVERS the granted tool reach: a component that cannot
  touch the host cannot call a granted host-backed tool either. The wasm-import
  path has to thread the granted tools back in as controlled host imports — a
  real design, not a flag.
- The canonical ABI still cannot lower Float / Map / resources / function-values
  across the boundary (see `quarantine.py` module docstring, verdict
  `deferred`), so a turn reaching a granted tool with such a signature has no
  boundary function yet.

What the path needs, concretely (TODO):

1. A canonical-ABI **host-import surface** for the granted services: each granted
   tool method presented to the sandboxed turn as a WASI-P2 imported function,
   marshalling the turn↔host call over the canonical ABI.
2. A per-turn **admit-to-substrate** entry on the crossing (an
   `AdmissionProfile.wasm` mode) that compiles the turn to a component against
   that host-import surface instead of refusing its externs.
3. Threading the sandboxed turn's granted-tool calls back onto the enclosing
   245 frame so commit/abort still govern them (the witnessed/emission crossings
   fire in the host providers, driven across the ABI boundary).

Until then, `no_extern` + the granted allowlist is the injection-resistant cut:
resistant for BOTH capability reach and smuggled host code, at the cost of the
untrusted turn writing no host code of its own (it composes granted tools only).
See the TODO in `src/revl/mcp/quarantine.py`.
