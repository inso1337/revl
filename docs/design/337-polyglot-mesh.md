# 337: polyglot admission mesh - re-admit at the seam

Design note for roadmap item 337 (`docs/v2.0-roadmap.md`, item 337), the
placement-tier payoff of the embeddable-compiler arc (items 332-338). The ask:
once every tier can embed the gate (item 332, one gate per tier), a component
that crosses a placement seam is RE-ADMITTED by the receiving tier's own
embedded compiler before it is accepted, so a heterogeneous fleet self-verifies
rather than trusting the wire. This is design-first. It changes no compiler code
here; it records the re-admit-at-the-seam protocol, the self-verifying-fleet
property and how it extends R2/R3 withdrawal, the dependency reality (the full
mesh is blocked on 332-per-tier, and 336 has confirmed the rust half of that
frontier is not built), a landable py-anchored slice, the G-invariant and the
honest boundary, an adversarial self-review that finds the critical, and a
sliced plan an implementation agent can pick up.

The doc grounds every claim in source that ships today: the gate surface
(`src/revl/gate.py`), the placement conductor (`src/revl/placement.py`), the
seam transport (`backends/python/bridge.py`), and the interop contract
(`docs/interop-bridge.md`, `docs/network-placement.md`).

## The problem: the conductor is a single point of trust

`revl run --placement` splits one composition across processes (local Unix
socket) or machines (item 56 network placement, TCP + mutual TLS). The
conductor does the compile ONCE, centrally:

- `run_placement` calls `compile_files(files)` a single time
  (`src/revl/placement.py`, `run_placement`), then slices the resulting IR per
  process.
- Each process is handed a `spec` dict and spawned. The spec carries the source
  paths (`"files"`), the component names this process must host
  (`"components"`), the provides/requires keys, the service method allowlists,
  and config (`src/revl/placement.py`, the `spec = { ... }` block).
- The receiving runner brings up its slice from that spec:
  `src/revl/_process_runner.py` loads its own components and serves/consumes the
  keys named in the spec. It does NOT re-compile or re-admit. It trusts that
  whoever produced the spec ran a compiler that admitted this code.

The seam authenticates the PEER but not the CODE. A network seam presents mutual
TLS (`docs/network-placement.md`, "Mutual TLS"): the provider's `serve` sets
`verify_mode = CERT_REQUIRED`, the consumer verifies the provider against the CA
and hostname. That answers "who is on the other end of the wire". It says
nothing about "is the component the other end is running one my own compiler
would admit". The stub also checks an allowlist (key + method must be declared
in the IR, `docs/interop-bridge.md` "What the stub will dispatch"), but the
allowlist is derived from the SAME centrally-compiled IR every process already
trusts; it is not an independent admission.

So today's trust model is exactly what `docs/interop-bridge.md` "Trust model"
states plainly: the placement file is "trusted input, at the same level as the
`.rvl` source", and the bridge is "a local development tool for a single
trusting user". One compiler admits; every tier trusts that verdict over the
wire. Item 337 removes that single point of trust: each receiving tier
re-admits what it is asked to run, with its own embedded gate, and rejects at
the seam if its own gate refuses.

## The building block that already exists: `swap_admission`

The mesh is not a new mechanism. The exact shape already ships for the live-swap
case in `src/revl/placement.py`, `swap_admission`: before a component is
re-hosted on a new tier, the candidate is recompiled AGAINST THE RUNNING
manifest and refused at the boundary if it does not link:

```
candidate = compile_files(list(files), manifest=running_ir,
                          replacing=(component,))
```

`swap_admission` returns `(candidate_ir, None)` on success or `(None,
diagnostic)` on refusal, "in which case the caller must leave the running
composition untouched (no blip)". That is re-admission at a topology change: the
same admission gate a py-tier hot-swap uses, applied before acceptance, refusing
without disturbing what is already live. Item 337 generalizes `swap_admission`
from "the swap driver re-admits before a cutover" to "every receiving tier
re-admits every component it accepts across a seam". The gate call is the item
332 surface (`admit_into`, below) instead of a raw `compile_files`, so the
decision is the tier's declared, versioned gate rather than an internal import.

## 1. The re-admit-at-the-seam protocol

A component crosses a placement seam when the conductor assigns it to a process
whose embedded gate is not the one that produced the shipped IR. The receiving
tier re-admits before it accepts. The handshake, py-to-py (Slice 1, below):

1. **Conductor compiles and slices** as today: `compile_files(files)` once, then
   per-process specs. The spec already carries `files` and `components`, so the
   source needed to re-admit travels with the assignment. The conductor's
   verdict is now advisory, not authoritative.

2. **Receiver re-admits from source, with its OWN gate.** Before
   `_process_runner` loads its `own` components, it calls its embedded gate over
   the source it was handed, against the manifest slice it is being asked to
   join:

   ```revl
   // sketch (fragment, not compiled): the receiver's boot-time seam gate
   let verdict = gate.admit_into(source_of(own), manifest = received_slice)
   if not verdict.admitted {
       reject_at_seam(verdict.code, verdict.message)   // do not come up
   }
   ```

   On py this is `revl.gate.admit_into(source, manifest)`
   (`src/revl/gate.py`), which is `compile_source(source, manifest=...)` behind
   the declared surface: the same admission `revl run` and truc's gatekeeper
   apply. The manifest argument makes the re-admission composition-aware, so a
   received component that would collide with the receiver's live slice (G2/G3)
   is refused with the collision's why-trace, exactly as `swap_admission`
   refuses a candidate that breaks a running consumer.

3. **The receiver decides acceptance on ITS verdict, never the sender's.** The
   wire may carry a claimed verdict for logging, but it is NOT an input to the
   acceptance decision. The receiver's own `admit_into` verdict is the only
   thing that gates coming up. If it refuses, the receiver rejects at the seam:
   the process does not enter its provide/serve loop, the seam is never
   established, and the conductor reports the refusal exactly as
   `swap_admission` reports one - no blip, nothing booted on the far side of the
   refusal.

4. **Verdict wire form is the one 332 already fixed.** When a receiver's gate is
   native (rust, later), its verdict crosses as the tier-agnostic
   `{admitted, code, message}` shape via `Verdict.from_native(wire)`
   (`src/revl/gate.py`): the `"<TAG>|<message>"` protocol split at the first
   `|`, message verbatim. So a py conductor can read a native receiver's
   rejection through the same structured shape it produces itself, and the seam
   rejection is legible fleet-wide without each tier inventing its own error
   envelope.

The essential inversion: acceptance flows from the RECEIVER's compiler, not the
sender's. The wire delivers source and identity; the receiving gate delivers the
verdict.

## 2. The self-verifying-fleet property, and how it extends R2/R3

**Each tier verifies what it receives.** A fleet of heterogeneous processes -
py here, node there, rust elsewhere - does not have one trusted compiler whose
say-so every node accepts. Every node runs its own gate over every component it
is asked to host, at the seam, before it accepts. The wire is a delivery
mechanism, not a trust boundary. This is the placement analog of the item 332
security clause ("the gate must NEVER admit what the reference refuses",
`src/revl/gate.py`) lifted to a distributed composition: no tier admits, at its
seam, what its own gate refuses.

**This extends R2/R3 with admission as the counterpart of withdrawal.** R2/R3
reactive withdrawal is the fleet's reaction to a peer LEAVING: a monitor
connection sees EOF when a provider dies, the proxy disposes its fiber, the
provision is withdrawn, and every dependent deactivates with ordered LIFO
teardown (`backends/python/bridge.py` header; `docs/interop-bridge.md` "Peer
death is withdrawal"). The reactive machinery already treats a seam as a live
edge that can change under the composition's feet, and responds by re-deriving
the committed view.

Item 337 adds the reaction to a peer ARRIVING (or being re-pointed): before a
provision is accepted across a seam, the receiver admits it; a provision that
fails admission is refused at the seam the same way a provision that dies is
withdrawn from it. Withdrawal removes on death; admission gates on acceptance.
Together they close the edge in both directions: a seam only ever carries a
provision the receiver both admitted (on arrival) and has not seen die (via the
monitor). The two share one discipline - the seam is never trusted as static -
and one refusal style: refuse without disturbing what is already live (the
`swap_admission` "no blip" rule).

## 3. The dependency reality: what is achievable now, what is blocked

The full mesh needs 332 ON EVERY TIER, because "the receiving tier's embedded
compiler" only exists where 332 has been landed for that tier. The current
state:

- **py gate: LANDED.** `src/revl/gate.py` ships `admit`, `admit_into`,
  `compile_to`, `gate_version`, and the `Verdict` shape, backed by the full
  reference compiler. `gate_version().frontier` is `reference-full:<language>`
  (`src/revl/gate.py`, `gate_version`). A py process can re-admit.
- **rust gate: NOT BUILT.** Item 336 (native single-binary tooling) confirmed
  the rust frontier: there is no `revl-gate` crate, and the gate `Verdict` has
  no source position on the native side. The rust embedded gate is a separate,
  larger deliverable that 332 explicitly defers ("A native (rust) layer-1 gate
  is a separate, larger deliverable (the `revl-gate` crate) and is NOT part of
  this module", `src/revl/gate.py`).
- **node/ts, wasm, java, go gates: NOT BUILT.** 332 defers "npm / wasm later"
  (item 335) and packages only the py wheel today. The node runner has a network
  CLIENT (item 149) but no embedded ADMISSION gate.

So the full cross-tier mesh is blocked on the SAME native-run / rust frontier
that blocked 336. The honest statement: 337 cannot deliver a rust or node
receiver that re-admits, today, because those receivers have no gate to re-admit
WITH.

**The achievable tier pair now is py-to-py.** Both sides run the landed py gate
(`src/revl/gate.py`), over the existing placement/bridge transport
(`src/revl/placement.py` local UDS, and the item 56 network path when both
endpoints are py). A py receiver re-admits with `admit_into` and rejects at the
seam on refusal. This is a complete, landable slice of the property: a
self-verifying seam between two py processes, no longer trusting the conductor's
single compile.

**py-to-node is NOT achievable now.** It would need a ts embedded-admit path
(`gate.admit` in the npm package), which 332 defers to item 335. Until an npm
gate exists, a node receiver cannot re-admit; it can only do what it does today
(trust the shipped spec). So the node consumer of a py provider that
`docs/network-placement.md` already allows stays a TRUSTING consumer under 337,
not a self-verifying one. State this at the seam: a seam whose receiver has no
embedded gate is a trusting seam, and 337 must MARK it as such rather than
imply a guarantee it cannot make (see the adversarial review, attack 3).

## 4. The G-invariant and the honest boundary

**G-invariant (what re-admission guarantees).** For every seam whose receiver
has an embedded gate, the received component was admitted by that receiver's own
gate, against the receiver's live manifest slice, before the seam was
established. No tier serves or consumes across a self-verifying seam a component
its own gate refuses. This is `swap_admission`'s "no blip" refusal generalized:
a failed re-admission leaves the receiver's running composition untouched and
establishes no seam.

**Honest boundary 1: re-admission is a COMPILE-TIME decision, not a sandbox.**
The gate admits or refuses source; it does not confine the admitted code's
runtime. `src/revl/gate.py` is explicit: "The gate cannot and does not claim to
confine its host; its guarantees govern the ADMITTED code." Re-admission at the
seam inherits exactly that boundary. It guarantees the receiver would compile
and admit this component; it does NOT jail what the component then does inside
the receiver's process. A component that admits and then, at runtime, does
something its (admitted) effects permit is not a re-admission failure. Runtime
confinement is the sandbox tier's job (item 411), orthogonal to this item.

**Honest boundary 2: a receiver can only admit what its own gate can compile
(the frontier gap).** A native gate pinned to the self-host corpus
(`gate_version().frontier` = the covered rows of `selfhost/compile.rvl`) covers
a SMALLER surface than the py `reference-full` gate. A component that uses a
language feature the receiver's gate does not cover cannot be re-admitted there
- not because it is unsafe, but because the receiver's compiler cannot compile
it. This is a real limit on the heterogeneous mesh: a component is placeable on
a tier only if that tier's gate frontier covers it. `gate_version().frontier`
(`src/revl/gate.py`) is the mechanism to DETECT this before trusting agreement,
and the seam must consult it (attack 2 below turns this from a limit into a
security hazard if the seam does not).

## 5. Adversarial self-review

Four attacks on the design. The critical one is attack 2; the sharpest concrete
implementation bypass is attack 4.

**Attack 1: a lying sender and a receiver that does not actually recompile.** A
sender ships an IR and a claimed verdict `admitted = true` for a component its
own compiler in fact refused (or was patched to skip). If the receiver's
"re-admit" step reads the wire verdict, or re-loads the shipped IR without
recompiling from source, the lie stands and the design buys nothing. DEFENSE:
the acceptance decision must be the receiver's own `admit_into(SOURCE,
manifest)` call over the source (from the spec's `files`), and the wire verdict
must never be an input to acceptance. This is enforceable and testable - the
exit test (Slice 1) ships a component whose source fails the receiver's gate and
asserts rejection regardless of any claimed verdict on the wire. Defensible, but
it is a standing implementation trap: any shortcut that trusts the shipped IR
re-opens the hole. The protocol section states the rule (step 3) precisely so an
implementer cannot read it as optional.

**Attack 2 (CRITICAL): version/frontier skew laundering a refusal into an
admission.** The whole property is "each tier verifies what it receives", but a
seam's safety is then only as strong as the RECEIVING gate. Suppose the fleet is
mixed-version: the sender runs a newer gate that refuses some construct (a
refusal added in language version N+1), and the receiver runs an OLDER gate
(version N) that never had that check. The receiver re-admits, its older gate
does not know the construct is refusable, and it ADMITS. Re-admission at a weaker
receiver has laundered a component the fleet's stricter gate refused into an
accepted one. The self-verifying-fleet property fails OPEN: a heterogeneous
fleet where a receiver's gate is not a safe superset of what the component needs
does not strengthen safety by re-admitting - it can weaken it. The 332 security
clause ("never admit what the reference refuses") is a PER-GATE promise at a
fixed version; it does not compose across versions on its own. The frontier gap
(honest boundary 2) makes this concrete: `gate_version()` returns `{api,
language, frontier}` precisely so two gates can detect they cover different
surfaces "before trusting their agreement" (`src/revl/gate.py`, `gate_version`),
and 337 is the caller that must do the detecting. MITIGATION, and it must be
fail-closed: a seam exchanges `gate_version()` and refuses when the receiver's
`language`/`frontier` is lower than or incomparable to the sender's - a lower or
unknown receiver frontier is a REFUSAL at the seam, never a silent accept. The
mesh must treat "the receiver re-admitted it" as sufficient ONLY when the
receiver's gate is a superset of the sender's; otherwise the seam is refused with
a frontier-skew diagnostic. Without this rule 337 is worse than trusting the
conductor, because it presents a self-verification badge while the weakest node
sets the fleet's real admission floor. This is the critical finding.

**Attack 3: the seam trusts the wire despite the design, wearing a TLS badge.**
Network placement authenticates the peer with mutual TLS
(`docs/network-placement.md`). A reader (or an implementer) may conflate
"authenticated peer" with "trusted code" and skip re-admission on an mTLS seam:
the sender presented a CA-signed cert, so why re-check its code. But mTLS
answers WHO, not WHAT. An authenticated sender whose key was stolen, or which is
itself faithfully running attacker-supplied source, ships code the receiver must
still re-admit. Authentication of the sender is orthogonal to admissibility of
the code. DEFENSE: re-admission is unconditional on a self-verifying seam,
independent of transport and independent of mTLS; the two are complementary
(mTLS attributes the seam to a named process, re-admission gates the code) and
neither substitutes for the other. Corollary, tied to section 3: a seam whose
receiver has NO embedded gate (a node receiver today) cannot re-admit at all, so
337 must MARK that seam as a trusting seam in the audit view rather than let mTLS
imply a guarantee that is not there.

**Attack 4 (sharpest concrete bypass): re-admission skipped on the R2/R3
re-point path.** Re-admission as designed runs at BOOT, before a process enters
its serve/consume loop. But the fleet has a runtime path that substitutes a
provider WITHOUT a boot: the `repoint` control command. `_process_runner` reads
newline-delimited control commands on stdin and handles
`{"op": "repoint", "key": "<k>", "socket": "<successor.sock>"}`, re-pointing a
proxy's `_Client` to a successor endpoint (`src/revl/_process_runner.py`;
`bridge._Client.repoint`, `backends/python/bridge.py`). This is a planned
cutover, the runtime twin of the R2/R3 withdrawal path. Crucially, `repoint`
carries a SOCKET, not source, and performs NO admission: the consumer re-points
to whatever now serves that address. So an attacker (or a buggy driver) who can
inject a `repoint` - or who can race a peer-death withdrawal and win the
re-point - substitutes an un-admitted provider at a seam the design claimed was
self-verifying. The `swap_admission` gate closes this for the CLI-driven
`revl swap` (it re-admits the candidate before `do_swap` re-points), but the raw
`repoint` control command on the runner's stdin has no such gate. This is a
real hole precisely where R2/R3 operates, which is the seam 337 is about.
MITIGATION: route the re-point acceptance through the same seam gate as boot -
the consumer re-admits the successor's contract (and, where source is available,
its source) before it re-points, so a `repoint` to an un-admitted successor is
refused at the seam exactly as a boot-time re-admission failure is. The re-point
path and the boot path must share one admission choke point, or the runtime
timing of R2/R3 becomes the way to bypass re-admission.

Attack 2 is the critical property-level finding (fail-open on frontier skew
defeats self-verification fleet-wide); attack 4 is the sharpest concrete
implementation bypass (the re-point control path is an un-gated seam today).

## 6. The sliced plan

**Slice 1 (achievable now, py-to-py, no new frontier): the receiver re-admits
at boot.** In `src/revl/_process_runner.py`, before loading `own` components,
call the embedded py gate `revl.gate.admit_into(source_of(own), manifest =
received_slice)` (`src/revl/gate.py`) and refuse to come up on a refusal,
reporting the seam rejection to the conductor the way `swap_admission` reports a
refused swap (no blip, nothing booted downstream). The acceptance decision is the
receiver's own verdict; any wire-carried verdict is advisory. Add the frontier
guard from attack 2 in its py-to-py form (both ends `reference-full:<language>`,
so the guard is a version-equality check that fails closed on mismatch),
establishing the discipline the later native tiers inherit. This slice touches
only py runners and the conductor, over the placement/bridge transport that
already ships. It is the item 337 exit test made real:

> Exit test (Slice 1): a `revl run --placement` run across two py processes
> where the receiving process is asked to host a component whose SOURCE fails
> its embedded admission (for example a draft with an open obligation, which
> `admit` refuses to run per `src/revl/gate.py`), and the receiver REJECTS at
> the seam - the process does not come up, no seam is established, the conductor
> reports the refusal - even when the shipped IR/verdict claims it was admitted.

**Slice 2 (deferred, blocked on 332-per-tier / native-run frontier): native
receivers.** A rust receiver re-admits with the `revl-gate` crate; the verdict
crosses via `Verdict.from_native`. BLOCKED: item 336 confirmed there is no
`revl-gate` crate and the native `Verdict` has no source position. This slice
cannot start until the rust gate lands (a 332 deliverable, itself downstream of
the self-host coverage frontier). Same blocker for node/ts (needs the npm gate,
item 335) and wasm/java/go.

**Slice 3 (deferred, needs Slice 1 + the runtime path): re-admit on re-point.**
Route the `repoint` control command through the boot-time admission choke point
(attack 4), so a runtime cutover is gated exactly as boot is. Depends on Slice 1
(the choke point must exist first) and is independent of the native frontier -
it is a py-tier hardening that can follow Slice 1 without waiting on Slice 2.

**Slice 4 (deferred): the audit view marks trusting vs self-verifying seams.**
Extend `revl audit --placement` (`src/revl/placement.py`, `sandbox_audit_view`)
so each seam reports whether its receiver re-admits (has an embedded gate at a
sufficient frontier) or trusts the wire (no gate, or a lower/incomparable
frontier). This makes attack 3's corollary visible: a mixed fleet shows exactly
which seams carry the 337 guarantee and which are still trusting seams, rather
than implying the guarantee fleet-wide.

## What ships in code from this note

Nothing yet; this is design-first, matching the arc's other notes
(`docs/design/332-embeddable-gate-api.md`). Slice 1 is the first landable change
and is bounded to the py runner and the conductor. The critical rule an
implementer must not drop: acceptance is the receiver's own gate verdict over
source (never the wire's), and the seam fails closed on frontier skew.
