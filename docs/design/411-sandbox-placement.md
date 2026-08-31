# 411: capability-enforced sandbox placement

Design note for roadmap item 411 (`docs/v2.0-roadmap.md:4152`): a component
runs in its own isolation boundary (wasm cell, container, or microVM) with a
declared capability envelope enforced at RUNTIME, still composed with
unsandboxed components over the item-363 seam. This is design-first. It
changes no compiler code; it records what already exists and how far it
already goes, the one architectural move the item turns on (isolation is a
third per-process placement dimension, not a new boundary), the manifest
surface, the per-rung boundary transport, the two-layer capability
enforcement model and its plan-time gate, the trust boundary stated
precisely, the guarantee accounting, the isolation ladder, the mixed-arch use
case, the conductor's sandbox-runtime driver, a staged plan, and exit tests
an implementation agent can pick up.

The item is the convergence of five existing threads, and this note builds on
each rather than reinventing any: item 363 supplies the placement mechanism
and the cross-process seam (`docs/design/363-per-component-tier-placement.md`,
landed in `src/revl/placement.py`); the capability model supplies the
statically checked envelope (`emission[caps]`, G4, attenuation, items
289/293/294); items 329 and 249 name the security payoff (containment that is
enforced, not just reviewed); item 337 and the distribution model make the
seam arch-agnostic, which is what the mixed-arch use case cashes in; and item
335's wasm tier is already a sandbox, which the isolation ladder positions as
its lightest rung.

## The problem (measured)

revl's capability story is checked, audited, and honest about where it stops.
Three measurements locate the gap exactly:

1. **The declared envelope is complete and static.** `emission[db]` bounds a
   provider's body to the boundaries it names (G4, `docs/capabilities.md`);
   the capability fixed point computes every component's real reach off the IR
   (`_emitting_capabilities`, `src/revl/lower.py:4274`, consumed by the G8
   audit surface at `src/revl/__main__.py:125-130`); spawn attenuation bounds
   lineage, child reach a checked subset of the parent's holdings
   (`_check_spawn_attenuation`, `lower.py:7972`;
   `docs/capability-attenuation.md`); and placement already refuses a
   component on a host that does not offer a capability it requires
   (item 119, `capability_realm_diagnostic`, `placement.py:532`). Four rules,
   four scopes, all compile- or plan-time.

2. **Nothing enforces any of it at runtime against host code.** The threat
   model's first stated non-goal: "the gate does not sandbox host code"
   (`docs/threat-model.md:119`; item 24). An `extern emission fn` body is
   arbitrary host code by design; G8 puts it on the audit surface and reads
   no further. Every placed process today runs with the conductor's full
   ambient authority: the py runner is `sys.executable -m
   revl._process_runner` (`placement.py:1562`), a plain child process with
   the invoking user's filesystem, network, and environment. A component
   whose audited reach is `emission[db]` runs in a process that can open any
   socket its author's uid can. The check and the enforcement live in
   different layers, and the enforcement layer is "trust the emitters and
   review the bodies".

3. **The enforced answer exists but not for live compositions.** The
   quarantine tier (item 45, `docs/quarantine-tier.md`,
   `src/revl/mcp/quarantine.py`) runs a CANDIDATE under wasmtime's component
   model, where confinement is physical and an escape is a trap. But it is a
   grading battery: it boots the candidate standalone in a throwaway
   sandbox and never wires it into a running composition. The 329 profile's
   no-extern cut refuses host code from an untrusted author outright, and
   its own design note defers the fuller answer, "allow a per-turn host body
   but make its confinement physical", as the wasm-import path
   (`docs/design/329-untrusted-author-profile.md:70-104`). Between "refuse
   all host code" and "grade it offline" there is no way to run a component
   of a LIVE composition inside an enforced boundary.

Item 411 is that missing shape: the statically declared capability envelope,
already computed and already gated at plan time, becomes the specification of
a runtime confinement the conductor requests from an isolation runtime, while
the component keeps composing with the rest of the system over the seam that
already exists.

## Background: what already exists, with the seams named

### The 363 conductor and its seam

Item 363 landed per-component tier placement, and every mechanism this note
extends is in `src/revl/placement.py` on main:

- `expand_tiers` (`placement.py:257`) turns a `[tiers]`/`default_tier`
  manifest into a synthesized one-process-per-tier `[processes]` topology; a
  classic manifest passes through byte-identical.
- The conductor compiles the whole composition once (`placement.py:1084`),
  so G2/G3/G4 are checked over the whole manifest before any split; the
  placement slice (`placement_slice`, `placement.py:730`) then narrows each
  process's build to the components it hosts plus what they reach.
- `tier_capability_gate` (`placement.py:819`) dry-runs each slice through
  its tier's emitter at plan time and refuses a component on a tier that
  cannot emit it, naming component, tier, and the tier's own reason. This is
  the template for 411's capability gate: a plan-time refusal in front of
  machinery that would otherwise fail late and raw.
- `cross_tier_boundary_check` (`placement.py:858`) refuses a resource-type
  crossing and names every sync cross-tier seam.
- The seam itself: per-process specs carry proxies (method list, async
  subset, seam deadline; `placement.py:1387-1423`) and a serve block whose
  method map is the stub's allowlist, so the served surface is exactly the
  service declaration's, G8 (`placement.py:1464-1477`). Transport is a UDS
  in a `0700` `mkdtemp` directory (`placement.py:1163-1166`), or TCP+mTLS
  when a seam names a machine (item 56, `placement.py:1196-1214`), with the
  per-role tier rule (mTLS provider py-only, consumer py/node,
  `placement.py:1300-1314`). Values cross by copy in the canonical wire
  encoding (`docs/interop-bridge.md:163-189`); a dead or wedged peer is a
  provider withdrawal driving R2/R3 (`docs/interop-bridge.md:222-228`), and
  the conductor watches each child's lifecycle through one pump thread per
  process (`placement.py:1626-1652`), tearing down with a
  terminate-then-kill ladder (`_stop_all`, `placement.py:1062-1079`).

The load-bearing fact, inherited verbatim from 363's crux: a component in its
own isolation boundary is a component in its own PROCESS, and cross-boundary
calls ride the seam that cross-process calls ride today. Item 411 adds no
transport, no encoding, and no new runtime boundary; it changes what stands
BETWEEN the process and the operating system.

### The capability model, and the one precedent that is almost this item

Item 119 is worth singling out, because its rule is 411's gate in embryo: a
host (process) declares the capabilities it OFFERS, and a component that
requires a capability may be placed only on a host that offers it, refused at
plan time naming component, capability, and host
(`docs/capability-realm-placement.md`; `capability_realm_diagnostic`,
`placement.py:532`). That is exactly the subset check 411 needs. What 119
does not do is make the offering MEAN anything at runtime: a host that
offers only `seal` does not, by any mechanism, deny the network to a
component placed on it. 411 is item 119 with teeth: the offering becomes a
grant the conductor translates into enforced confinement, and the refusal
stays where 119 put it.

The other capability rules stack unchanged underneath: G4 bounds a provider
to its declaration, attenuation (item 66) bounds spawned lineage (and closes
reach over the spawn graph, so a sandboxed component's children are counted
inside its envelope by construction), and item 289's wasm least-authority
chain, `host imports subset-of declared caps subset-of policy-allowed`
(`docs/v2.0-roadmap.md:3793`), is the wasm-tier statement of the same
discipline this note generalizes: the runtime-visible surface must be
derived from, and bounded by, the declared one. Items 293 (evidence bundles)
and 294 (resource-bounded capabilities, `docs/v2.0-roadmap.md:3801,3806`)
are future refinements this design leaves room for and does not depend on.

### The confinement precedents

The wasm tier's defining property is that the paradigm is enforced by the
sandbox: a component's coeffect specification IS its import section, its
provision IS its exports, and confinement is the instruction set
(`backends/wasm/emit.py:1-8`). A wasm module has no filesystem and no
network unless the embedder grants an import. Item 45 runs untrusted
candidates there; item 335 compiles the gate itself there. Neither wires a
confined component into a live placement, and 363 explicitly refused wasm as
a placement TIER (no wasm placement runner on the substrate,
`placement.py:1372-1377`). The isolation ladder below resolves that tension
without reopening 363's refusal: the wasm CELL is an isolation an ordinary
py process hosts, not a placement tier.

Items 329 and 249 are the demand side. 329's delivered cut forbids an
untrusted author host code entirely (`no_extern`,
`src/revl/admit_profile.py`); 249 tracks untrusted DATA through granted
channels (`docs/design/249-taint-provenance.md`). Both are checks over
declarations and flows; neither can do anything about a host body that lies.
The payoff section below states exactly what enforcement adds to each.

## The crux: a third placement dimension, and two capability layers

Two decisions shape everything downstream.

**Isolation is a per-process placement property.** A process already has a
tier (which runtime executes it, item 363) and optionally a machine (where
it runs, item 56) and an offered-capability/realm profile (item 119). The
sandbox is a fourth column in the same row: what the process may reach in
the operating system. It composes with the tier rather than replacing it (a
sandboxed process still picks py/node/rust/go/java; the artifact and runner
are unchanged), and it composes with the seam rather than bypassing it (the
sandboxed process serves and consumes exactly the proxies and stubs its spec
describes). The alternative, a per-COMPONENT in-process confinement
mechanism, is refused for 363's reasons plus one: every OS isolation
primitive on offer (namespaces, VMs, wasm instantiation) confines a process
or an instance, not a co-resident object graph, so a per-component
in-process sandbox would be a fiction the runtime cannot enforce. One
consequence is stated rather than hidden: components that share a sandboxed
process share its envelope. That is the same sharing rule co-located
components already have for machine and tier, and the `[tiers]`-form sugar
below defaults a sandboxed component into a process of its own.

**The capability envelope has two layers, and they are checked by different
parties.** revl capabilities are boundary NAMES: requirement keys and extern
names (`docs/capabilities.md`, section 2). An OS sandbox grant is stated in
resource terms: filesystem paths, network reach. The two meet at a
distinction the IR already makes. Partition a sandboxed component's computed
reach (the G4 fixed point, closed over spawns per item 66):

- **Seam-served capabilities**: required keys whose provider lives in
  another process. These need NO OS grant: the crossing rides the seam
  socket, which is the one channel every sandbox grants by construction,
  and the far side's own placement governs what happens there. A no-net
  sandboxed component can still call `db.execute` if `db` is served across
  the seam; that is the composition working as designed, and it is what
  makes a sandboxed component USEFUL rather than merely inert (the 329 note
  named severed tool reach as pure confinement's failure mode,
  `docs/design/329-untrusted-author-profile.md:87-88`).
- **Host-rooted capabilities**: extern names the component reaches, plus
  `*` for the opaque residue (a bare `emission`, a host reach no key can
  name). These are the ones that touch the OS from INSIDE the boundary, and
  they are what the grant is about.

The static layer checks names: a host-rooted capability whose declared
resource needs exceed the sandbox grant is refused at plan time, the item
119/363 discipline. The runtime layer enforces resources: whatever any host
body actually attempts, named, opaque, or lying, is bounded by the envelope
the isolation runtime applied. The two layers are deliberately redundant in
one direction: the static gate catches honest mistakes before anything
spawns; the envelope catches everything else, loudly, at the attempt. The
trust-boundary section states who enforces what and who is trusted.

## Surface: how a component declares its sandbox and envelope

Following 363's decided precedent (manifest data, no `.rvl` grammar; the
verdict chain at `docs/interop-bridge.md:372-377`, item 119's
"no source change", and 363's option-(b) adoption), the whole surface is the
placement manifest.

### The `[processes]` form (full control)

```toml
[processes.worker]
backend = "py"
components = ["Worker"]

[processes.worker.sandbox]
isolation = "container"        # "wasm-cell" | "container" | "microvm"
image = "revl-runner-py:3.12"  # container/microvm: the image, pin by digest
# the OS envelope. Both default to deny: no key means none granted.
fs  = []                       # or ["/data/incoming:ro", "/scratch:rw"]
net = "none"                   # or "all"; a host allowlist is a named
                               # follow-on (see the staged plan)
```

A process without a `sandbox` table is byte-identical to today, the
342/363/396 additivity discipline. Every component in a sandboxed process
shares the envelope (stated above); an author who wants distinct envelopes
writes distinct processes.

### The `[tiers]` form (sugar)

The `[tiers]` manifest gains a parallel `[sandbox]` table:

```toml
default_tier = "py"

[tiers]
HotWorker = "rust"

[sandbox]
Untrusted = { isolation = "container", image = "revl-runner-py:3.12" }
```

Expansion extends `expand_tiers` (`placement.py:257`): a `[sandbox]`-listed
component is split OUT of its tier group into its own synthesized process
`sandbox_<component>` carrying the sandbox table (envelope keys default to
deny-all as above), and everything else groups per tier exactly as today. A
component named in `[sandbox]` but not in `[tiers]` takes `default_tier`.
The both-forms refusal (`[sandbox]` alongside `[processes]`) mirrors the
existing `[tiers]`-vs-`[processes]` rule (`placement.py:273-279`).

### The needs table (what a host-rooted capability requires)

G8 makes host bodies opaque, so the conductor cannot infer that extern
`fetch`'s body opens a socket. The manifest declares it, top-level like item
119's `[capabilities]` table, keyed by capability name (an extern's needs do
not depend on where it runs):

```toml
[sandbox.needs]
fetch    = ["net"]
scratch  = ["fs:/scratch:rw"]
```

An absent entry means "this extern's body needs nothing beyond CPU and
memory". That default is deliberate: forcing an entry for every reached
stdlib extern (`json`, `str` helpers) would bury the signal in boilerplate,
and a wrong or missing entry is not a hole, because the entry is a CLAIM the
runtime envelope does not consult; a body whose undeclared reach exceeds the
grant fails loudly inside the sandbox at the attempt (the enforcement
section). The needs table exists so the gate can refuse the honest
mismatches at plan time and so `revl audit` can print, per sandboxed
component, which reached externs are vouched self-contained.

### The rejected source-level spelling

```revl sketch
component Untrusted sandboxed(no-fs, no-net) {
  provides work: Work
  ...
}
```

Refused, for 363's option-(a) reasons verbatim (grammar spend against the
manifest-data precedent, tier/deployment portability, staleness against
runtime placement changes) plus one of its own: an isolation boundary is a
deployment trust decision, made by whoever operates the composition, about
code they may not have authored. The author of an untrusted component is
exactly the wrong party to control whether it is sandboxed. The manifest is
trusted input at the level of source (`docs/interop-bridge.md`, "Trust
model"), written by the operator; that is the right hands.

## The boundary, precisely

A sandboxed component composes over the existing seam; the seam crosses the
isolation boundary via a per-rung transport variant. Everything the seam
guarantees carries over unchanged, because the specs, proxies, stubs, and
runners are unchanged: value-copy canonical encoding, the G8 stub allowlist,
per-operation seam deadlines, and peer death as provider withdrawal.
"Peer death" gains one new cause, sandbox death (a container OOM-killed, a
VM torn down, a cell trapped fatally), and it needs no new mechanism: the
driver section arranges for sandbox death to be child-process death, which
the conductor's pump already converts into DOWN and the reactive cascade
handles as withdrawal, R2/R3.

Per rung:

- **Container.** The conductor's placement directory (the `0700` mkdtemp
  holding the sockets, `placement.py:1163-1166`) is the seam's home; the
  driver bind-mounts into the container a PRIVATE per-process view of it: a
  directory containing exactly the socket the process serves (created by
  the runner inside, appearing on the host through the mount) and the
  sockets it consumes, nothing else. Mounting the whole directory would
  hand every sandboxed process a peek at every seam, a shared-filesystem
  side channel the narrow mount closes. The container runs as the invoking
  uid (`--user`), preserving the same-OS-user access model the `0700`
  directory already relies on (`docs/interop-bridge.md`, "Who may call
  across a seam"). The seam mount is exempt from the `fs` grant by
  construction: it IS the composition channel, not a filesystem capability.
- **microVM.** No shared filesystem by default, so the UDS variant is out;
  the seam crosses the VM's virtio network as TCP+mTLS, which is item 56's
  existing transport with the conductor minting per-process identities
  (`generate_seam_certs`, `placement.py:134`, exists for exactly this
  loopback-mint shape). The VM's network grant is then "seam endpoints
  only" at minimum: `net = "none"` still permits the mTLS seam, enforced by
  the VM's network config admitting only the conductor-designated
  host:ports. The item-56 per-role tier rule carries over and is restated
  as a 411 constraint: a microVM-sandboxed process that PROVIDES across the
  seam must be a py process (the mTLS listener is py-only,
  `placement.py:1300-1307`), and a consumer must be py or node
  (`placement.py:1308-1314`), until the network-placement follow-ons land.
- **Wasm cell.** In-process, so there is no transport in the OS sense: the
  hosting process is an ordinary PY placement process (the existing
  `_process_runner`) that instantiates the component's wasm module under an
  embedded wasmtime, and the "seam crossing" is import satisfaction. The
  cell's coeffect imports (`coeffect:<key>`, `backends/wasm/emit.py:20-21`)
  are satisfied by host functions that forward onto the process's ordinary
  seam proxies; its exports are dispatched by the process's ordinary stub.
  This is the 329-deferred wasm-import path landing as a placement citizen,
  and it deliberately dodges 363's wasm refusal: the placement TIER is py
  (an existing runner; nothing about `KNOWN_BACKENDS` changes), the
  ISOLATION is wasm. The cell rung inherits the wasm tier's emit
  restrictions (i64/i32 boundary discipline, no Float/Map at the boundary,
  no config channel; `backends/wasm/emit.py:29-41`), checked at plan time
  by extending the 363 dry-run gate to run `backends/wasm/emit.py` over the
  cell component's slice, the same oracle discipline with the same
  no-second-list rationale.

One boundary rule is new and matters: a sandboxed process's spec must not
carry secrets the envelope pretends to withhold. The spec file today is
written into the placement directory and handed to the runner
(`placement.py:1640-1642`); for a sandboxed process the driver passes only
that process's spec through the mount, and config values destined for OTHER
processes never enter the boundary (they already do not; the per-process
spec carries the shared `config` table today, `placement.py:1442`, and
narrowing it to the process's own components' config is a small,
independently landable tightening this item takes as a stage-1 side task,
because handing a confined untrusted component the whole composition's
config would undercut the point).

## Capability enforcement and the plan-time gate

The gate runs in `run_placement` next to its siblings (item 119's checks at
`placement.py:1181-1188`, the 363 gates at `placement.py:1569-1583`), before
anything spawns. For each sandboxed process P with envelope E:

1. **Compute reach.** Union the G4 fixed-point reach of P's components
   (`_emitting_capabilities`, `lower.py:4274`; spawn-closed per item 66),
   and partition: seam-served (required keys owned by another process, read
   off the same `owner` map the proxies use, `placement.py:1144`) versus
   host-rooted (reached extern names, plus `*`).
2. **Subset check, the refusal.** For every NAMED host-rooted capability
   `c`, look up `[sandbox.needs]`; if the declared needs of `c` are not
   covered by E (a `net` need against `net = "none"`, an `fs:path` need
   with no covering mount), refuse at plan time naming the component, the
   capability, the need, and the missing grant:

   ```
   error: component 'Untrusted' cannot run in sandbox 'worker': capability
          'fetch' needs net, but the sandbox grants net = "none"; grant it
          ([processes.worker.sandbox] net = "all"), serve it across the seam
          instead, or move the component out of the sandbox
   ```

   This is item 119's refusal shape (`placement.py:561-567`) with the offer
   made enforceable, and 363's gate discipline (refuse at plan, name the
   parties, never a runtime stack trace as the user-visible surface).
3. **The opaque residue, reported not refused.** `*` in the host-rooted set
   (an unscoped `emission`, a body no name bounds) is never a refusal under
   a sandbox, because bounding it is exactly what the envelope is FOR: the
   static layer cannot say what an opaque body needs, and does not have to,
   since the runtime envelope binds it regardless. The boot summary
   (extending `placement.py:1594-1595`) prints it:

   ```
   placement: worker[py, container: net=none fs=none]=[Untrusted]
     sandbox worker: reach * (opaque host surface), bounded only by the
     envelope; seam-served: db, log; vouched self-contained: json_parse
   ```

4. **Attenuation composes for free.** A sandboxed component's spawned
   children run in its process, inside its envelope; item 66 already
   refuses a child reaching beyond the parent (`lower.py:7972`), and reach
   is spawn-closed, so step 1 counted them. No new lineage rule.

What the runtime then enforces is the other layer: the envelope, applied by
the isolation runtime at launch, bounds every actual attempt. A body whose
undeclared or lying reach exceeds the grant fails host-natively inside the
boundary (a refused `connect` in a no-net container, a read-only filesystem
error, a wasm trap on a missing import), surfacing in the component's own
extern frame as a loud error, at worst becoming sandbox death, which is
withdrawal. The wasm cell makes this layer STATIC again, and that is item
289 realized by placement: the cell's import section is GENERATED from the
grant (seam proxies plus granted host functions and nothing else), so an
ungranted reach is not a denied syscall but a missing import, refused at
instantiation by the substrate itself, the full
`host imports subset-of declared caps subset-of policy-allowed` chain with
the placement manifest as the policy.

Item 294, when it lands, upgrades step 2 without changing its shape:
resource-bounded capability declarations (`network.call(host=...)`) would
make `[sandbox.needs]` derivable from the IR instead of author-declared,
and the subset check becomes bounded subsumption. The gate is written
against "declared needs of c", which is exactly the slot 294 refines.

## The trust boundary, precisely

This section is the honesty the item demands, stated once and cited from
everywhere else.

**What revl guarantees.** The plan-time gate: a placement whose declared
capability needs exceed the sandbox grant never spawns, with a diagnostic
naming component and missing grant. The request: the conductor derives the
exact confinement flags from the manifest (`--network=none`, `--read-only`,
the mount list, `--cap-drop=ALL`; the VM network config; the wasm import
set) and prints them in the boot summary, so what was REQUESTED is
reviewable and diffable, the same audit discipline as G8. The seam: the stub
dispatches only declared methods, values cross by copy, deadlines bound
every crossing, and sandbox death is withdrawal. And on the wasm rung, the
grant-to-import derivation, which is checkable by reading the emitted
module's import section (a conformance test, not a trust statement).

**What revl trusts.** The isolation runtime (docker/podman, the microVM
monitor, wasmtime) is a TRUSTED ENFORCER: revl requests `--network=none`
and cannot verify the daemon applied it, has no view of kernel namespace
implementation, and cannot rule out runtime bugs or kernel escapes. This is
not a new kind of trust; it is the same kind extended to one more party.
Today every guarantee revl states already rides on an unverified CPython,
Node, JVM, and OS ("the gate defends the declaration, not the runtime",
`docs/threat-model.md:131-140`); the sandbox runtime joins that list, one
line lower in the stack. The practical mitigations are operational and
named, not guaranteed: pin images by digest (a mutable tag is an arbitrary
code vector; the image is trusted input at the level of the placement file),
prefer the stronger rung for the more hostile component (the ladder), and
let item 293's evidence bundles carry attestation when they land.

**What confinement does not do even when perfectly enforced.** It bounds
REACH, not misuse of granted reach. A sandboxed component can still
exfiltrate through any capability it IS granted, including the seam: a
confined component with a seam-served `log` key can smuggle data into log
lines, and a no-net envelope does nothing about it. That residual belongs to
item 249 (untrusted data through granted channels) and to the item-33
composition policy, both of which keep their jobs; the payoff section is
precise about the division. Covert channels (timing, resource exhaustion)
are likewise out of scope, as they are for every isolation runtime this
design can request.

## The security payoff: what 329 and 249 gain

**Item 329, from refused to confined.** The delivered profile forbids an
untrusted author host code outright (`no_extern`), which is sound and
limiting: the model may only compose pre-granted services. With 411, the
deferred second mode becomes a placement: admit the untrusted-author
component WITH its host bodies, place it in a deny-all sandbox (the wasm
cell for a per-turn compile, a container for a longer-lived component), and
wire its granted tool surface as seam-served keys. The G8 sentence changes
from "we cannot see inside the host body, review it" to "we cannot see
inside the host body, and it does not matter for reach: the body holds no
authority beyond the granted seam". An opaque or actively malicious body
confined to `net = "none"`, `fs = []` can compute, call its granted
proxies, and nothing else; its escape attempt is a refused syscall or a
trap, not an incident. The granted-tool-reach problem that made pure
quarantine unusable (`329-untrusted-author-profile.md:87-88`) is solved by
the seam being the one granted channel: severed from the host, connected to
the composition.

**Item 249, the enforcement floor under the flow analysis.** Taint tracking
reasons about where untrusted data flows; its class-(a) sinks and witnessed
operations assume the component boundary means something at runtime. A
sandbox gives the analysis a floor it can cite: a witnessed or
class-(a)-handling component placed in a no-net sandbox cannot leak the
value through an unanalyzed host path, because there is no host path; the
only egress is the seam, which the analysis DOES see (seam crossings are
declared operations on declared services). Concretely: today a component
that endorses untrusted input relies on its own host bodies being honest;
sandboxed, the endorsement boundary's TCB shrinks to the declared surface
plus the isolation runtime. 249's residual (misuse of granted channels)
remains 249's, stated above; what 411 removes is the UNDECLARED channel.

Neither item's checks weaken or change: 411 adds an enforcement layer under
them, it does not replace a line of either.

## Guarantees across the boundary

Parity with 363, restated tersely with the one new cause:

- **G2/G3/G4: linked once, before the split.** The conductor compiles the
  whole composition in one `compile_files` call (`placement.py:1084`);
  sandbox assignment, like tier assignment, is invisible to the linker. The
  same composition links or refuses identically under any `[sandbox]`
  assignment: a property to test, not an accident.
- **Effects, witnessed frames: per process, exactly as today.** Effects
  accumulate in the process that performs them; teardown is LIFO-complete
  per process; cross-process ordering is the reactive cascade. A witnessed
  frame does not span the seam, and a fortiori does not span a SECURITY
  boundary: a rollback that reached into a sandbox would be a channel
  violating the envelope's direction of trust, so cross-sandbox witnessed
  rollback is scoped out with 363's exact rationale plus that one. An
  author who needs cross-boundary compensation writes it as service
  operations the audit sees.
- **Determinism (385): scoped out across the sandbox boundary, harder.**
  363 scoped cross-tier byte-agreement to the conformance corpus; a
  sandboxed FOREIGN binary (the mixed-arch case) is not even in the corpus,
  is not reproducible by revl, and makes no byte-agreement promise. The
  385 discipline continues to bind the revl-emitted artifacts on both
  sides; it says nothing about a wrapped alien.
- **Withdrawal: sandbox death is peer death.** Container OOM-kill, VM
  teardown, a fatal cell trap: all become child exit, DOWN, withdrawal,
  R2/R3, replacement re-activation. Exit-tested.
- **Swap: refused for sandboxed components in v1.** `revl swap` moves a
  component across tiers under an admission gate (`swap_admission`,
  `placement.py:340`); moving one across an ISOLATION boundary changes the
  security posture of a running system, which is an operator decision this
  item declines to automate. A swap naming a sandboxed component (or
  targeting a sandboxed process) is refused with the named gap; lifting it
  is a follow-on with its own admission story.

## The isolation ladder

One capability contract (the `sandbox` block, the needs table, the gate),
three enforcement strengths. The `isolation` key picks the rung; nothing
else in the surface changes.

| rung | boundary | strength | cost | when |
|---|---|---|---|---|
| `wasm-cell` | wasm instantiation inside a py host process | reach is statically absent (no import, no call): the strongest STATIC story, item 289's chain realized; but in-process, so a wasmtime bug is a host compromise, and the wasm tier's emit restrictions bound what can live there | near-zero launch cost; per-call marshal at the import boundary; sync base calculus | per-turn untrusted-author code (329), small pure-ish logic, high-churn admission |
| `container` | OS namespaces, shared kernel | kernel-enforced fs/net/pid isolation; whole-tier artifacts run unchanged (any backend, full stdlib) | image build/pull, ~100ms-1s launch; shared-kernel attack surface | opaque third-party host bodies, mixed-arch wrapping, the default hostile-component rung |
| `microvm` | own kernel under a VM monitor | strongest OS-level boundary; hostile-multitenant grade | slowest launch, memory floor per VM, seam over TCP+mTLS only, network-role tier limits | regulated/multi-tenant isolation, code you assume is actively escaping |

Reading the ladder honestly: strength is not totally ordered. The cell is
weakest against a runtime bug (in-process) and strongest against ambient
reach (no syscall surface at all); the microVM inverts both. The contract
being uniform is the point: an author hardens a component by changing one
manifest word, and the gate, seam, and guarantees are identical on every
rung.

## Mixed-arch compositions (item 337 and the distribution model)

The seam carries serialized values in the canonical encoding and nothing
else (`docs/interop-bridge.md:163-189`); no pointer, ABI, or instruction
set crosses it. That makes the container rung an arch bridge for free: a
foreign-arch binary, wrapped as a revl component (its operations exposed as
externs, via inline bodies or an item-396 ref, that shell into or link
against the binary), runs in a container OF ITS ARCH and composes with the
host-arch placement over the unchanged seam.

Surface: the sandbox block gains `platform = "linux/arm64"` (the
OCI platform string); the driver passes `--platform` and the container
runtime supplies emulation (qemu/binfmt, Rosetta) or a native node. The
image carries the tier's placement runner built for that platform; the spec,
proxies, and stub protocol are byte-identical, because nothing in them is
arch-aware. The conductor's preflight extends to "can this daemon run this
platform" (one diagnostic, not a spawn failure).

Stated costs: emulated execution is slow (an emulated hot worker is a
performance lie the boot summary should name, the 363 sync-seam-report
discipline applied to platform); the 385 determinism scope-out is doubly
binding (stated above); and the foreign binary is trusted input squared,
the image-pinning note applying with force. Item 337's re-admission at the
seam is the complementary future: the sandbox confines what the foreign
component can DO, 337 would verify what it IS at the seam; they share hooks
and neither waits for the other.

## Lifecycle: the sandbox-runtime driver

The conductor's process machinery is deliberately reused whole. The driver
is a translation layer at the three existing seams:

- **Launch** (`command_for`, `placement.py:1548-1562`, plus `spawn` at
  `placement.py:1640-1652`): for a sandboxed process, wrap the tier's
  command. Container: `docker run --rm -i --name revl_<placement>_<p>
  --user <uid> --network=none --read-only --cap-drop=ALL <mounts>
  <platform> <image> <tier command>`, with the flag set derived from the
  envelope and printed. The `docker run` CLIENT is the conductor's child
  process: stdout pumps through the existing `pump` thread (UP/DOWN/
  REPOINTED lines unchanged), stdin gives the rust stop mode a path, and
  signal proxying makes `_stop_all`'s terminate reach the runner inside.
  MicroVM: the monitor process is the child, same shape. Wasm cell: no
  wrapper at all; the py runner receives a `cell` spec key and instantiates
  the module with the generated import set.
- **Health**: no new mechanism. Sandbox death is client-process death; the
  pump and the existing poll see it; withdrawal follows. The driver adds
  one hardening: a teardown-path `docker rm -f <name>` (and the VM
  equivalent) for the wedged case where the client dies but the sandbox
  lingers, mirroring `_stop_all`'s kill fallback (`placement.py:1075-1079`).
- **Teardown**: `_stop_all` unchanged in shape; graceful stop reaches the
  runner (LIFO teardown runs INSIDE the boundary, so no-residue proofs are
  produced where the effects lived), then the force ladder, then the
  lingering-sandbox sweep. `--rm` keeps the happy path clean.
- **Preflight** (`_preflight`, `placement.py:1131`): a placement using
  containers checks the daemon and image availability up front, one
  diagnostic; `revl doctor` (item 291) grows rows for the isolation
  runtimes.

The reconciliation with 363's conductor is thus one sentence: a sandboxed
process is one more entry in `children` whose command happens to launch a
jail around the same runner, and every conductor behavior downstream of
spawn (pump, swap bookkeeping, teardown, probes, boot summary) operates on
it unmodified.

## Non-goals, held against pressure

- **No automatic sandboxing.** The manifest declares; no heuristic decides
  a component "looks untrusted". The distribution model's non-goal
  discipline (`docs/distribution-model.md:128`) applies: declared placement,
  never inferred.
- **No syscall-level policy language.** The envelope is capabilities and
  coarse resources (fs mounts, net), not seccomp profiles; an operator who
  needs finer policy configures the runtime out of band. Resource QUOTAS
  (cpu/memory) are a natural `sandbox` key but are scheduling, not
  capability, and land as a follow-on with item 294's resource-bounded
  story.
- **No verification of the enforcer.** Stated in the trust boundary; no
  stage of the plan claims otherwise.
- **No cross-boundary witnessed rollback, no cross-sandbox determinism
  claim, no swap of sandboxed components in v1.** Each named above with its
  rationale; each has this section to argue with.

## Staged implementation plan

Each stage lands independently; a placement with no `sandbox` surface stays
byte-identical throughout (the 342/363/396 additivity discipline).

- **Stage 1 (surface + gate).** Parse `[processes.<p>.sandbox]`, the
  `[tiers]`-form `[sandbox]` table and its expansion, `[sandbox.needs]`;
  the plan-time capability gate (reach partition, subset refusal, opaque
  report); the per-process config narrowing; boot-summary and `revl run
  --plan` lines; `revl audit` prints the envelope and the vouched list.
  Running a sandboxed placement is a clean refusal naming the gap (the 396
  discipline) until stage 2. Exit: the no-net-vs-net-need refusal names
  component, capability, and grant; a sandbox-free placement is
  byte-identical through specs, builds, and boot output.
- **Stage 2 (container boundary, py tier).** The driver's launch wrapper
  for `isolation = "container"` on py processes: envelope-to-flags
  derivation, the private seam mount, uid mapping, preflight, teardown
  sweep; a first-party runner image recipe. Exit: a sandboxed py component
  composes with an unsandboxed process, a probe's call crosses the
  boundary and returns; `docker kill` on the sandbox withdraws the
  consumer (R2/R3) and teardown proves no residue; an exfil-attempt body
  (socket connect) inside `net = "none"` fails loudly in its extern frame
  while the composition keeps running.
- **Stage 3 (all compiled tiers + mixed-arch).** The wrapper generalized to
  node/rust/go/java runner images; `platform` and the emulation preflight;
  the boot-summary emulation note. Exit: a foreign-platform container
  serves a key to a host-arch consumer over the seam (the mixed-arch
  demo); the tier gate still names tier refusals first.
- **Stage 4 (wasm cell).** The py runner's cell mode: instantiate the
  component's wasm module with imports generated from the grant (seam
  proxies plus granted host functions only); the plan-time wasm-emitter
  dry-run for cell components; the import-section conformance check (the
  289 chain, asserted by reading the emitted module). Exit: a cell
  component calls a seam-served key and cannot name any other import; an
  ungranted-reach body fails at instantiation, not at call time.
- **Stage 5 (microVM).** The TCP+mTLS seam variant inside the VM network
  config, conductor-minted identities, the monitor as child process; the
  per-role tier restatement. This stage is the largest and may split; if
  it stalls, `isolation = "microvm"` remains a clean named-gap refusal,
  never a weaker container silently substituted.
- **Named follow-ons, out of scope:** a net HOST allowlist (needs an egress
  proxy or netfilter story; `net` stays `"none" | "all"` until it is
  designed, refusing an allowlist spelling with the gap named); swap
  in/out of sandboxes; resource quotas; 294-derived needs; 337 seam
  re-admission; 293 image/evidence attestation.

## Exit tests

- **Plan-time refusal (the headline):** a component reaching an extern with
  `[sandbox.needs] fetch = ["net"]`, placed in a `net = "none"` sandbox,
  is refused before anything spawns, naming component, capability, need,
  and grant; the same manifest with `net = "all"`, or with the extern's
  provider moved out of the sandbox, boots.
- **Composition across the boundary:** a sandboxed component and an
  unsandboxed one compose over the seam; a `--once` probe's cross-boundary
  call returns its value; teardown proves no residue on both sides.
- **Enforcement is real:** a deny-all sandboxed component whose host body
  attempts a network connect gets a host-native loud failure inside its
  extern frame; the compositions's other processes are unaffected; nothing
  silently succeeds.
- **Sandbox death is withdrawal:** killing the container withdraws the
  consumer reactively (R2/R3); a replacement re-activates it; a wedged
  seam call breaches its deadline as the distinguishable error, not a
  hang.
- **Additivity:** every placement without a `sandbox` surface is
  byte-identical through expansion, specs, builds, boot output, and
  teardown; `expand_tiers` on a `[tiers]`-only manifest is byte-identical
  to before this item.
- **Linker blindness:** a G2 collision refuses identically under any
  `[sandbox]` assignment; sandbox keys never reach the checker.
- **Mixed-arch:** the foreign-platform container serves a key to a
  host-arch consumer; an ADT and a `Result` cross and rebuild (the
  outcome fixtures re-run across the platform boundary).
- **The cell's import section:** the emitted cell module's imports are
  exactly the seam proxies plus granted host functions (asserted by
  parsing the module); an ungranted host reach is a missing import at
  instantiation.
- **Swap refusal:** `revl swap` naming a sandboxed component is refused
  with the named gap; the running composition is untouched.
- **`test_doc_examples` stays green:** the one proposed-syntax block in
  this note is fenced `revl sketch` (it must not compile unless a source
  spelling ever lands, which this note refuses); manifests are TOML
  blocks the gate does not compile.

## The honest hard part (consolidated)

The item's good fortune is 363's: the boundary, the seam, the gate
discipline, and the per-process conductor all exist, so the design is a
fourth placement column, a translation layer at spawn, and one new
plan-time check, and every guarantee statement above is parity with the
cross-process status quo. The genuinely hard residues are four. First, the
vocabulary bridge: revl capabilities are boundary names and OS grants are
resources, and the mapping between them (`[sandbox.needs]`) is an
author-declared claim G8 prevents anyone from verifying statically; the
design's answer is layered honesty (the gate refuses declared mismatches
early, the envelope bounds undeclared ones loudly at runtime), and that
answer must be kept from quietly inverting into "the needs table is
checked, so it must be true". Second, the trusted enforcer: revl requests
confinement and cannot verify it was applied, and this note's precision
("trusted the way the OS is today") will be under pressure to blur into a
stronger claim in every summary of the feature; the trust-boundary section
exists to be cited against that. Third, the net allowlist gap: the most
requested envelope ("only api.example.com") is exactly the one container
runtimes do not natively enforce, so v1 ships `none`/`all` and a named
follow-on, and holding that refusal against the obvious pressure to fake it
with an unenforced flag is a scope discipline, not an oversight. Fourth,
the microVM rung inherits every network-placement limit at once (py-only
listener, py/node consumers, cert plumbing), which makes the strongest rung
the most constrained one for now; the plan isolates it in its own stage so
the container and cell rungs, which carry the security payoff for 329/249,
never wait on it.
