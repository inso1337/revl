# 337: polyglot admission mesh - re-admit at the seam

Design note for roadmap item 337 (`docs/v2.0-roadmap.md:4313`), the
placement-tier payoff of the embeddable-compiler arc (items 332-338, foundation
`docs/design/332-embeddable-gate-api.md`). The ask: once every tier can embed
the gate (332, one gate per tier), a component crossing a placement SEAM is
RE-ADMITTED by the receiving tier's own embedded compiler against the receiver's
local running manifest before it is accepted, so a heterogeneous fleet
self-verifies instead of trusting the wire. Trust does not cross a boundary: a
compromised or un-admitted provider cannot be substituted at any seam.

This is design-first. It changes no compiler code. It records: the general mesh
(which seams re-admit and how), what identity each seam carries so admission can
actually run at the receiver, the fail-closed defaults, the trust model (each
tier is its own trust domain), an adversarial self-review that finds the single
most serious flaw and closes it, and an honest split of what needs the rust
`revl-gate` crate (per-tier native admission) versus what is expressible in the
reference (py) layer today.

Crucially, ONE INSTANCE OF THIS MESH JUST LANDED, and this design generalizes it
rather than re-deriving it. The `repoint` control command a placement process
receives on stdin is now gated through admission
(`src/revl/_process_runner.py`, the `_repoint_decision`/`_apply_repoint` seam;
`src/revl/placement.py` `swap_admission`, `:710`). The whole design is: name the
invariant that landed seam already satisfies, and require every OTHER seam to
satisfy the same one.

## The problem: the conductor is a single point of trust

`revl run --placement` splits one composition across processes (local Unix
sockets) or machines (item 56 network placement, TCP + mutual TLS). The
conductor compiles ONCE, centrally, then slices the IR per process:

- `run_placement` calls `compile_files(files)` a single time
  (`src/revl/placement.py`), then hands each process a `spec` dict carrying the
  full composition source paths (`spec["files"]`), the components this process
  hosts, the provides/requires keys, the method allowlists, and config.
- The receiving runner brings up its slice from that spec
  (`src/revl/_process_runner.py`). Historically it did NOT re-admit: it trusted
  that whoever produced the spec ran a compiler that admitted the code.

The seam authenticates the PEER, not the CODE. A network seam presents mutual
TLS (`docs/network-placement.md`, "Mutual TLS"): the provider's `serve` sets
`verify_mode = CERT_REQUIRED`, the consumer verifies the provider against the CA
and hostname. That answers "who is on the other end of the wire". It says
nothing about "is the component the other end runs one my own compiler would
admit". The stub's allowlist (`docs/interop-bridge.md`, "What the stub will
dispatch") is derived from the SAME centrally-compiled IR every process already
trusts; it is not an independent admission. `docs/interop-bridge.md`, "Trust
model", states it plainly: the placement file is "trusted input, at the same
level as the `.rvl` source", and the bridge is "a local development tool for a
single trusting user". One compiler admits; every tier trusts that verdict over
the wire.

Item 337 removes that single point of trust: each receiving tier re-admits what
it is asked to run or accept, with its own embedded gate, and rejects at the
seam if its own gate refuses.

## The landed instance: the repoint re-admission seam (the template)

The `repoint` control command re-points a live proxy `_Client` from its current
provider onto a successor socket, the planned-cutover half of `revl swap`
(distinct from the peer-death withdrawal `bridge._Client` drives, which stays
untouched). As it shipped originally, the command carried a socket and no source
(`{"op": "repoint", "key": k, "socket": succ}`) and the process re-pointed the
proxy with no admission step. So an injected or raced `repoint` could substitute
an UN-ADMITTED provider at a placement seam, bypassing the `swap_admission` gate
the CLI swap path runs on the conductor. That gap is now closed:

```
{"op": "repoint", "key": "<k>", "socket": "<succ.sock>",
 "component": "<c>", "backend": "<tier>"}
```

The command now carries the successor's ADMISSIBLE IDENTITY (`component`,
`backend`), and the receiving process re-admits that identity against its OWN
running manifest before accepting the cutover
(`_process_runner.py::_repoint_decision`):

```python
# _process_runner.py (landed), the seam decision, verbatim shape
component, backend = cmd.get("component"), cmd.get("backend")
if not component or not backend:
    return False, "repoint carries no admissible successor reference ... refused fail-closed (item 337)"
_candidate, error = swap_admission(list(spec_files), running_ir, component, backend)
if error is not None:
    return False, error.splitlines()[0]
return True, None
```

Four properties are load-bearing, and they ARE the mesh invariant:

1. **The gate inputs are receiver-controlled, not wire-controlled.** The wire
   carries a SELECTOR (`component` + `backend`). The receiver's own running
   manifest is `running_ir = compile_files(spec["files"])`
   (`_process_runner.py`, computed once at boot from the source every spec
   already carries), and `swap_admission` re-compiles the named component's
   source AGAINST that manifest (`compile_files(..., manifest=running_ir,
   replacing=(component,))`, `placement.py:749`). Both inputs to the gate come
   from state the receiver already trusts; the wire supplies only which
   component to re-admit and onto which tier.
2. **Fail closed with no admissible reference.** A legacy socket-only command
   (no `component`/`backend`) is REFUSED, never silently accepted. Absence of a
   reference is a refusal, not a wave-through.
3. **No blip on refusal.** On a refused decision `_apply_repoint` returns False
   and the proxy keeps serving its CURRENT target; the running composition is
   untouched, exactly `swap_admission`'s "leave the running composition
   untouched (no blip)" contract.
4. **Full re-admission, not a re-link of a wire IR.** `swap_admission` runs the
   real admission gate (front end + the cross-tier seam gate
   `resource_crossing_refusal` + the re-pointed-sync refusal), the same gate
   boot and `revl swap` use. The receiver does not trust a compiled IR handed to
   it; it re-derives the verdict from source under its own compiler.

The test that pins it (`tests/test_swap.py`, item 337 block) drives the seam
directly: a successor that fails admission (a sync `cache` service swapped to the
rust tier cannot cross the process seam) is REFUSED, and the refusal BLOCKS the
substitution: `_apply_repoint(...)` returns False and the stub client is never
re-pointed, so it keeps its original target.

## The generalization: which seams re-admit, and the one invariant they share

A "seam" is any point where a component or a service reference crosses from one
tier's trust domain into another and the receiving side begins to depend on it.
There are three, and the mesh requires the same invariant at each.

**The seam invariant (the whole design in one sentence).** A receiving tier
accepts a crossing only after its OWN embedded gate re-admits the crossing
component, by COMPILING FROM SOURCE (or the native crate's `admit(source)`)
under that tier's gate, against a manifest the RECEIVER independently holds;
absence of an admissible reference is a refusal; a refusal leaves the receiver's
running slice untouched. The landed repoint seam satisfies this exactly; the
other two must be made to.

### Seam 1: placement repoint (landed, py)

The live cutover above. Identity on the wire: `{key, socket, component,
backend}`. Manifest: the receiver's `running_ir` from `spec["files"]`. Gate:
`swap_admission`. Status: shipped on py.

### Seam 2: the cross-tier bridge at boot (expressible today, py)

When the conductor first brings up the fleet, each process consumes keys served
by OTHER processes as bridge proxies (`_process_runner.py`, step 1: "load the
keys it must consume from other processes as bridge proxies"). Today the consumer
trusts that the provider process is running admitted code because the conductor
compiled once. Under the mesh, the CONSUMER process re-admits the provider
component it is about to bind a proxy to, against the consumer's own running
manifest, before it wires the proxy, the same call `_repoint_decision` makes but
at initial wiring rather than at cutover. Because every spec already carries
`spec["files"]` (the full composition source) and the provider `component` name,
the consumer already holds everything the gate needs; the boot-time re-admission
is `swap_admission(spec_files, running_ir, provider_component,
provider_backend)` with no new transport. This is expressible in the reference
layer today (it is the landed seam moved one event earlier), and the design
recommends it as the second py slice.

The honest limit: at boot the consumer holds the same centrally-sliced source
the conductor used, so a boot-time re-admission on py catches a RACE or an
INJECTED wiring (a proxy pointed somewhere the manifest does not sanction) and a
tier-seam violation, but it cannot catch a provider whose ACTUAL running bytes
differ from the source the consumer holds; detecting that is Seam 3's remote
problem, below. Re-admitting what you can independently derive is sound and
worth doing; claiming it verifies bytes you never saw would not be.

### Seam 3: the remote handoff (frontier: needs a per-tier native gate + a trust anchor)

Network placement (item 56) spans machines that need not share a filesystem or a
language. Two sub-cases:

- **The receiver HOLDS the crossing component's source** (a homogeneous fleet
  distributing one repo). Identical to Seam 2: re-admit the selector against the
  receiver's own manifest. Expressible today on py; on a non-py receiver it needs
  that tier's embedded gate (the `revl-gate` crate for rust, 335's wasm gate at
  the edge).
- **The receiver does NOT hold the source** (a genuinely foreign component,
  authored elsewhere, arriving over the wire). Here the receiver cannot resolve a
  selector against its own source, so the crossing must carry SOURCE, and the
  receiver re-admits that source with its OWN gate against its OWN running
  manifest. This is sound about the receiver's half of the link (a candidate that
  does not safely link into the receiver's running world is refused) but it does
  NOT by itself establish that the source is what the remote will actually run;
  that requires an out-of-band trust anchor (attestation / signing / the item-293
  evidence bundle), named here and deferred, never smuggled in as if
  re-admission alone verified provenance.

What each seam carries, tabulated:

| seam | identity on the wire | manifest admitted against | gate | status |
|------|----------------------|---------------------------|------|--------|
| 1. repoint cutover | `{key, socket, component, backend}` | receiver `running_ir` from `spec["files"]` | `swap_admission` (py) | LANDED |
| 2. bridge boot wiring | provider `component` + `backend` (from spec) | receiver `running_ir` from `spec["files"]` | `swap_admission` (py) | expressible today (py) |
| 3a. remote, source held | `component` + `backend` | receiver-held manifest | native gate per tier | frontier (crate/wasm) |
| 3b. remote, source foreign | source + `component` + `backend` + attestation | receiver-held manifest | native gate per tier | frontier + trust anchor |

## What identity a seam carries, and why it is a SELECTOR not an IR

The landed seam's sharpest decision is that the wire carries a `component` +
`backend` selector, not a compiled IR and not (by default) source. This is not
an optimization; it is the soundness property, and the adversarial review turns
on it. A selector names WHICH admissible thing the receiver should re-derive and
check; the receiver derives the actual gate inputs (source, manifest) from state
it already trusts. An IR on the wire would be the sender's front-end judgment
handed to the receiver to re-link, which is precisely the transitive trust 337
exists to remove (the receiver would inherit the sender's parse/check/lower
verdict). Source on the wire (Seam 3b) is admissible only when the receiver
CANNOT hold it AND an independent anchor vouches for provenance; even then the
receiver re-runs its own FULL front end on that source, never a re-link.

`gate_version()` (`src/revl/gate.py:261`) is the second piece of seam identity.
It returns `{api, language, frontier}`; on py `frontier` is
`reference-full:<language>`, on a native crate it is the self-host corpus
frontier. A seam re-admission is only meaningful if the two ends agree on what
surface each gate covers: a receiver whose gate covers LESS than the sender's
(a native frontier receiver behind a reference sender) may REFUSE a crossing the
sender admitted, and that is the safe direction (fail closed, an inconvenience).
The dangerous direction is a receiver whose gate would ADMIT what the reference
refuses; the fail-closed clause below forbids it. The seam therefore carries and
compares `gate_version()` so skew is detectable before agreement is trusted; this
is exactly what 332 built the version surface for and what 335 promised to carry
across the edge.

## Fail-closed defaults

Three, inherited from the landed seam and made general:

1. **No admissible reference is a refusal.** A crossing that arrives without a
   selector (or, for Seam 3b, without source + anchor) is refused, never
   accepted on trust. The landed `_repoint_decision` does exactly this for the
   socket-only legacy command.
2. **An admission error is a refusal.** `_repoint_decision` wraps the gate call
   in a bare `except Exception` that returns `(False, ...)`: any failure to
   REACH a verdict fails closed. A tier whose embedded gate is absent, stale, or
   erroring refuses the crossing rather than waving it through.
3. **A refusal never disturbs the running slice.** No-blip: the receiver keeps
   serving its current topology on any refusal, so a hostile crossing cannot even
   cause a denial-of-service blip by being rejected. This is `swap_admission`'s
   "leave the running composition untouched" carried to every seam.

And the arc-wide security clause, stated in 337's terms: **never admit what the
reference refuses.** A native tier's gate may refuse a crossing the reference
admits (frontier-scoped, an inconvenience); it may NEVER admit a crossing the
reference refuses (the defect class the whole arc exists to prevent). Each tier's
gate ships behind the 332/335 differential corpus as a release gate, and the
mesh adds nothing weaker at the seam.

## The trust model: each tier is its own trust domain

The mesh reframes the fleet: every tier process is a distinct trust domain that
trusts NO verdict it did not itself produce. Consequences stated precisely:

- **Trust is not transitive across a seam.** Tier A admitting a component gives
  tier B nothing. B re-admits with B's gate against B's manifest before it
  depends on A's component. A's compromise (a corrupted A gate, an A that skipped
  admission, an attacker who owns A's process) cannot smuggle an un-admitted
  provider into B, because B's acceptance is gated on B's own re-derivation, not
  on A's claim.
- **The peer authentication and the code admission are orthogonal and both
  required.** Mutual TLS answers "who is on the wire"; seam re-admission answers
  "would my own compiler admit what I am about to depend on". Neither substitutes
  for the other; the mesh adds the second without touching the first.
- **The blast radius of a compromised gate is one tier.** Because each tier
  re-admits, a single corrupted gate can wave through crossings only into ITS OWN
  process; it cannot make peers accept anything, since peers re-check. This is
  the distributed analogue of 334's single-gate blast-radius reasoning, inverted
  into a containment property.
- **What the mesh does NOT establish** (the honest boundary): it does not confine
  a tier's host process (a library never jails its own host, the 332 contract),
  it does not verify that a remote's running BYTES match admitted source without
  an attestation anchor (Seam 3b), and it does not sandbox a granted extern's host
  body (that is 411's runtime confinement, `docs/design/411-sandbox-placement.md`,
  the wasm-cell/container/microVM ladder). Re-admission decides; enforcement of
  the isolation an admitted-but-untrusted host body needs is 411's lane, and 337
  composes with it (an edge that re-admits at the seam AND runs the crossing in a
  wasm cell is the full story) without overlapping it.

## Relationship to R2/R3 reactive withdrawal

The item text says 337 extends R2/R3 reactive withdrawal with admission. The
existing reactive path (`backends/python/bridge.py:23-50`, `docs/interop-bridge.md`)
already distinguishes two events at a seam: peer DEATH is withdrawal (a monitor
connection sees EOF, `on_lost` fires, every dependent deactivates with ordered
teardown, R2/R3), and peer REPLACEMENT is re-point (`_Client.repoint(new_path)`
swaps the target under the same proxy without firing `on_lost`). 337 inserts
admission on the REPLACEMENT edge only: a planned cutover now passes the
receiving tier's gate before the re-point takes effect, so "carry the provision
to a successor" becomes "carry it to an ADMITTED successor or keep the current
one". The withdrawal edge is untouched, deliberately: a provider vanishing is not
a substitution to admit, it is a loss to react to, and conflating them would make
a dying peer look like a rejected candidate. The mesh is the admission layer over
the reactive layer, not a change to it.

## Landed today vs the rust/self-host frontier (honest slice split)

**Expressible in the reference (py) layer today:**

- Seam 1 (repoint cutover re-admission). LANDED (`_process_runner.py`,
  `placement.py`, `tests/test_swap.py`).
- Seam 2 (boot-time bridge re-admission on py). The same `swap_admission` call
  moved to initial proxy wiring; no new machinery, no new transport, since every
  spec already carries the source and the provider component name. This is the
  recommended next py slice.
- The seam-identity discipline (selector not IR; `gate_version()` skew check).
  All reference-layer, all shippable on py now.

**Needs the rust `revl-gate` crate (per-tier native admission), 332/336 frontier:**

- Any NON-py receiver re-admitting at a seam needs THAT tier's embedded gate. On
  py the gate is `compile_files`/`swap_admission` in-process. On rust the gate is
  the 332-designed `revl-gate` crate's native `admit(source)`, which item 332
  DEFERRED building (`docs/design/332`, "Deferred: rust revl-gate crate") and
  which item 336 confirmed is not yet built. A rust receiver cannot self-verify a
  crossing until that crate exists.
- Native `admit_into` (admission against a running MANIFEST) does not exist on
  the native pipeline at all: `selfhost/compile.rvl` has no manifest parameter
  (`docs/design/332`, `docs/design/333` dependency table). So even with the crate,
  a rust receiver's Seam-2/Seam-3 re-admission (which is manifest-spanning) is
  blocked on growing native manifest admission, a named self-host frontier stage,
  not an implied one. This is the sharpest honest limit: the crate's `admit` cut
  is STANDALONE admission; seam re-admission is INTO a running manifest, and that
  half is not native yet.
- The wasm edge gate (335) re-admitting a fetched component at an edge seam is the
  same pattern one tier further out, gated on 335's own frontier (cordis-rs on
  wasm32, `Map` value model).

The mesh is therefore FULLY EXPRESSIBLE on a homogeneous py fleet today (Seams 1
and 2), and a heterogeneous fleet's non-py seams are gated on the per-tier gate
frontier the arc already tracks. The design does not pretend the polyglot mesh is
buildable end-to-end now; it pins the invariant, ships the py seams, and names
the exact native stages the rest waits on.

## Adversarial self-review

The single most serious flaw, and the fix, first; then the lesser attacks.

### CRITICAL: re-admitting a wire-carried IR against a wire-influenced manifest is admission theater

**Attack.** The item text says the receiving tier "re-admits the crossing
candidate against the local running manifest". The tempting, wrong
implementation reads "the crossing candidate" as the compiled IR the sender puts
on the wire (it is right there, already parsed, cheap to re-link) and "the local
running manifest" as the composition the sender describes in the same crossing
message. An earlier sketch of this very item reached for `gate.admit_into(source,
manifest)` at the seam with both arguments arriving over the wire. Under that
shape a COMPROMISED sender controls BOTH inputs to the gate that is supposed to
check it: it crafts a candidate IR and a manifest against which that candidate
trivially links, hands both across, and the receiver's `admit_into` returns
"admitted". The receiving tier ran its gate and learned nothing, because the
attacker chose the question. Worse, re-LINKING a wire IR (rather than
recompiling from source) means the receiver inherits the sender's front-end
parse/check/lower judgment: A's verdict leaked into B unchanged. This is exactly
the transitive trust across a boundary that 337 exists to remove, dressed up as
its opposite; and because the seam is the mesh's whole security claim, getting it
wrong ships the hole under the banner.

**Why it is the CRITICAL.** Every other property of the mesh is downstream of
"the seam re-admission is real". If the gate's inputs are attacker-controlled,
fail-closed defaults, the trust-domain model, and the R2/R3 admission layer are
all theater over a gate that answers a question the attacker wrote. And it is the
easy mistake precisely because the wire already carries an IR (the bridge speaks
serialized values) and carrying source + recompiling looks like redundant work.

**Resolution (mandatory, and it is exactly what the landed seam does).** The wire
carries a SELECTOR (`component` + `backend`), never the IR the gate consumes and
never (by default) the manifest. The receiver derives BOTH gate inputs from state
it independently holds: the manifest is `running_ir = compile_files(spec["files"])`
from the receiver's own trusted source, and the candidate is RE-COMPILED FROM
SOURCE by `swap_admission` (front end included: `compile_files(..., manifest=
running_ir, replacing=(component,))`), never a re-link of a wire IR. The attacker
controls only WHICH component the receiver re-admits and onto which tier; it
controls neither the source the receiver compiles nor the manifest it links
against. For Seam 3b (a foreign component whose source the receiver genuinely
cannot hold), source may travel, but (a) the receiver re-runs its OWN full front
end on it, never a re-link, (b) the manifest is still receiver-owned, and (c) an
out-of-band attestation anchor must vouch for provenance, or the crossing is
refused fail-closed rather than admitted-on-trust. The invariant is stated once
and enforced at every seam: **the gate's inputs are receiver-controlled; the wire
carries a selector, at most source-with-attestation, never the manifest and never
an IR to re-link.** The landed repoint seam is the proof this is buildable; the
generalization's only job is to not weaken it.

### A2: a seam re-admits against a STALE manifest (a race with a concurrent swap)

**Attack.** A crossing is re-admitted against `running_ir` computed at boot, but a
concurrent swap has since changed the receiver's running topology, so the seam
admits against a manifest that no longer describes reality.

**Assessment / mitigation.** The landed seam computes `running_ir` once at boot
because a placement process's own slice does not change under it (a swap re-points
its PROXIES, handled by the repoint seam itself, but does not recompile the
process's own hosted components). Where a receiver's manifest CAN change (future
in-process generational swaps), the seam must re-derive `running_ir` from the
current generation at decision time, not cache it across a topology change; the
rule is "admit against the manifest that is live at the instant of acceptance",
and the no-blip refusal means a lost race is a refusal (safe), never a
substitution. This is a real implementation obligation for the manifest-mutating
case, named so it is not silently assumed away.

### A3: skew - a native receiver's gate covers less surface than the reference sender

**Attack.** A rust receiver on the self-host frontier re-admits a crossing the
reference sender admitted; the frontier gate refuses a construct it does not
cover, and a naive fleet reads the refusal as a security rejection and halts.

**Assessment / mitigation.** This is the SAFE divergence direction (fail closed):
a refusal is never a false-admit. The mesh carries `gate_version().frontier` on
the seam so the receiver's refusal is ATTRIBUTABLE ("refused: construct outside
this tier's frontier `selfhost:...`") rather than mysterious, and a fleet can
route a frontier-refused crossing to a reference-gate fallback the way 335's
serverless case does. The forbidden direction (native admits what reference
refuses) stays closed by the 332/335 differential release gate. Skew is made
detectable and routable, not defined away.

### A4: the receiver's own embedded gate is the corrupted component

**Attack.** The attacker owns the receiver process and patches its embedded gate
to `return admitted`, so seam re-admission passes everything.

**Assessment.** Out of the threat model, and the trust-domain model already says
so: a host that owns a tier's process can reimplement that tier's gate, exactly as
332's contract states ("the gate's guarantees hold when the host uses the
published surface"). The mesh's CONTAINMENT property is what survives this: a
corrupted gate waves things through into its OWN process only; peers re-admit with
their own gates, so the blast radius is one tier. `gate_version()` lets a paranoid
peer assert the surface/language/frontier of a gate before trusting agreement, but
337 does not claim to defend a tier against its own compromised host, and says so.

## Exit tests

- **The item's own exit.** A cross-tier `revl run --placement` run where the
  receiving tier REJECTS, at the seam, a component that fails its embedded
  admission, and keeps serving its current topology (no blip). The landed repoint
  test (`tests/test_swap.py`, item 337) is this exit for Seam 1 at the
  handler/admission level; the full-run version drives it through the conductor.
- **Selector, not IR.** A seam handed a crafted IR + manifest pair that would
  link is NOT admitted on that basis: the receiver re-compiles from its own source
  against its own manifest, so a wire IR is ignored as a gate input. Proven by a
  test where the wire IR disagrees with the receiver's source and the receiver's
  verdict follows its SOURCE.
- **Fail closed.** A crossing with no admissible reference (socket-only, or
  foreign source with no attestation) is refused; an admission that raises is
  refused; a refusal leaves the receiver's slice byte-identical.
- **No transitive trust.** A crossing admitted by a (simulated compromised) sender
  is independently REFUSED by the receiver when the receiver's own gate refuses
  it, demonstrating the receiver's verdict does not inherit the sender's.
- **Skew attributable.** A frontier receiver's refusal of an out-of-surface
  crossing carries the `gate_version().frontier` in its diagnostic.
- **Additivity.** The full suite and every placement/swap golden are
  byte-identical with the boot-time Seam-2 re-admission present; a homogeneous py
  fleet's behavior is unchanged except that an injected/raced repoint is now
  refused.
- **`test_doc_examples` stays green**: every revl-ish block here is `sketch`- or
  plain-fenced and must not compile until the feature lands.

## The honest hard part (consolidated)

Four costs, taken in the open. First, seam re-admission is only as independent as
its inputs: the whole value collapses if the gate admits a wire-supplied
candidate against a wire-supplied manifest, so the design's one non-negotiable is
that the receiver controls both inputs (selector on the wire, source and manifest
re-derived locally), which is exactly why it grounds itself in the landed repoint
seam rather than the `admit_into`-over-the-wire shape an earlier sketch reached
for. Second, "re-admit the crossing candidate" is honest only where the receiver
can independently derive what it admits: a homogeneous fleet (Seams 1 and 2) can,
a foreign remote component (Seam 3b) cannot without an attestation anchor this
item names and defers, and pretending re-admission alone verifies bytes you never
saw would be the overclaim. Third, the polyglot half is gated on the per-tier
gate frontier the arc already tracks: the mesh is fully expressible on py today,
a rust receiver waits on the deferred `revl-gate` crate, and manifest-spanning
native admission (`admit_into` on the self-host pipeline) does not exist yet, so
the crate's standalone `admit` cut is not by itself enough for a rust seam.
Fourth, the mesh re-admits, it does not confine: a tier that owns its host can
corrupt its own gate (contained to one tier by the re-admit-everywhere property,
not prevented), and a granted extern's host body still needs 411's runtime
isolation, which 337 composes with and does not replace.
