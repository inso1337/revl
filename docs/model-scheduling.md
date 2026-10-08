# Model scheduling onto declared devices

Roadmap item 515 (issue #1189). A `model role` can declare the device it
needs, and a `route model` arm can name an ordered set of candidate roles
(`docs/design/539-model-portfolio.md`). This page covers the other half: a
placement host declares the devices it offers, and before anything spawns the
conductor picks, for every routed model action, one candidate that fits. If
no candidate fits, the placement is refused.

The code is `src/revl/model_schedule.py`, called from `run_placement` in
`src/revl/placement.py`, and `src/revl/model_placement.py` in the child. The
tests are `tests/test_model_schedule_515.py` and
`tests/test_model_schedule_handoff_515.py`.

## The two declarations

The program declares the demand. This is item 515's flagship program,
unchanged:

```revl
model role fast  on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory  512 quant int8
model role cloud off_device

service Answer { fn classify(text: Str) -> Str }

component Classifier provides out: Answer {
  route model on classify {
    confidential -> fast | small,
    * -> cloud
  }
  provide out { fn classify(text) = text }
}
```

The placement file declares the supply, one table per device, on the process
that hosts the component:

```toml
[processes.edge]
components = ["Classifier"]

[[processes.edge.devices]]
name = "gpu0"
device = "gpu"
memory_mib = 8192
quantisation = ["q4_k_m"]

[[processes.edge.devices]]
name = "cpu0"
device = "cpu"
memory_mib = 16384
quantisation = ["int8", "fp16"]
```

Each device carries exactly four keys:

| key | meaning |
| --- | ------- |
| `name` | what the schedule calls this device; unique on the host |
| `device` | `cpu`, `gpu` or `npu`, the same closed vocabulary a role declares against |
| `memory_mib` | the memory the device offers to model loads, a positive integer |
| `quantisation` | the quantisation tags the device can load, a non-empty list |

The last three are the field names `model_profile.declared_floor` uses for the
demand, so the comparison is field by field.

## The decision

A candidate fits a device when the device class is the same, the role's
`quant` tag is in the device's `quantisation` list, and the device still has
`memory` MiB free. A `gpu` role never lands on an `npu` or a `cpu`.

For each host, every routed action of every component placed there is one
step, in program order:

- A role arm is satisfied by one of its candidates. They are tried in the
  order written, and within a candidate, devices are tried in the order the
  host declares them. An arm may opt in to residency instead — see
  [One arm may opt in to residency](#one-arm-may-opt-in-to-residency) — and an
  arm that does not is ordered by the written set however the host reports.
- A council arm is satisfied only when every member role is placed, because a
  council asks every member.
- A role is loaded once per host. A later step that picks a role already
  resident reuses its device and takes no more memory. Two components routing
  to `fast` on one host share one load; the same role on two hosts is two
  loads, one per host.

The search backtracks. If giving the first action its preferred candidate
leaves no room for a later action, the first action moves to its fallback
instead of the later action being refused. The first complete assignment in
that order is the answer, so an earlier action gets its preference before a
later one does, and the same inputs always give the same schedule. The search
is exhaustive up to 10,000 placement attempts and refuses past that rather
than admitting a schedule it did not finish.

With the placement above, the conductor prints:

```
  model schedule [edge]: Classifier.classify confidential -> fast on gpu0
  model schedule [edge]: Classifier.classify * -> cloud off_device, reserves nothing on this host
```

The same program on a host that offers only a 1024 MiB CPU prints:

```
  model schedule [pi]: Classifier.classify confidential -> small on cpu0, fallback 1 of 1
```

## One arm may opt in to residency

An arm may close with `prefer resident`:

```revl fragment
route model on classify {
  confidential -> fast | small prefer resident,
  * -> cloud
}
```

That arm's author is saying a candidate the host **already holds** may beat an
earlier-written one. The clause is on one arm and there is no block-level,
host-level or command-line form of it, so it can never reorder a second arm and
never becomes a default: the written order stays the preference for every arm
that does not write it, and for every arm of every program written before the
clause existed (design note 539 §11.6 item 2, over decision 12).

What residency changes is **which candidate an opted-in arm settles on**. It
does not change the candidate set, and it does not change the memory the plan
reserves for the role it picks. The candidates the host holds are tried first,
each group still in the order the program wrote it; the rest follow, and a held
candidate that does not fit is a miss like any other, so the arm falls back
through the order it always had. `rank` in the decision and in the printed
fallback is still the position in the **written** set, so the line reads
`small on cpu0, fallback 1 of 1` whether or not residency moved it there.

The clause is refused where it would rank nothing, because an accepted-but-inert
clause reads as a requirement without being one:

- on an arm that names one candidate, which has no order to change;
- on an arm that reaches a council, which is one candidate whose aggregation
  the council declares.

The plan reads what a host holds at plan time: acquisition, item 1's second
half. When `--providers` names the bindings **and** an arm this placement
schedules wrote the clause, `revl.providers.plan_time_residency()` asks each
bound server's own `/api/ps` what it holds for the candidate roles of those
arms, and derives the device **class** from the server's own memory report.
`run_placement` hands that to `_model_schedules`, which is the seam the clause
ranks through. Nothing is asked otherwise, not one request and not even the
configuration read, so a composition that does not use the clause plans
exactly as it did, and `--providers` on its own moves no verdict.

A server that cannot be asked is a **refusal**, not a default: the run exits
non-zero before anything spawns, naming the host, the server and the role.

Two limits are worth stating plainly. The read is a report about a moment, and
revl cannot tell who loaded what, so a model another client loaded is
indistinguishable from one revl provisioned; that is why the printed note says
the decision is not reproducible from the composition alone. And `/api/ps`
names memory, not a device, so what a server read yields is a device class.
Only the keys of a residency are read, which is what lets the two producers
(a landed provision's `timeline` through `resident_roles()`, and a plan-time
server report) be interchangeable. The schedule a host ranked on carries that
residency into the spec, so the child re-derives the **same** decision instead
of a second one.

The provision-record reader (`Provisions.residency()` through
`model_schedule.resident_roles()`) still has no plan-time caller: it reads a
timeline that already happened, so it serves a re-plan over live provisions
rather than a fresh plan, which is why the plan-time read is a separate one.

The self-host gate reads the clause too, and carries it on the arm
(`apref` in `selfhost/lower.rvl`). Its arm reader (`model_arms_in`) consumes an
optional trailing `prefer resident` after a candidate set and passes the
preference to `model_order_roles`, which is `_Search.order` for the residency
this gate is handed — none, since it is given a program and not a host. An
opted-in arm is therefore decided rather than refused; every other trailing
shape (a bare `prefer`, a bare `resident`, the clause on a one-candidate arm, on
a council arm, or followed by anything else) leaves a token the reader cannot
account for and the whole block is refused **by name** — `` `route model on
classify` in Classifier is written in a form this gate does not decide`` —
rather than stepped over. Fail-closed in the one direction that matters: the
gate never admits a clause the reference refuses, and it never steps over a
token it did not read.

Because the gate is handed no residency, the clause moves no verdict here. It
cannot: the fold it feeds (`model_reach_comp`) refuses when *any* edge offends,
and a reorder is a permutation of the same edges; and every candidate set the
gate *decides* is residence-uniform by item 515's own rule, so "could this host
hold it" answers the same for every candidate of a decidable arm. What the
clause changes for this gate is the answer it gives — an opted-in arm is read
and decided instead of refused by name — and `_model_schedules`'s `residency`
parameter, which the placement conductor does now fill, stays the seam a
host-aware fold would plug into.

## The refusal

When no candidate fits, `run_placement` exits non-zero before anything
spawns, and names the host, the action, the origin, and why each candidate
missed:

```
error: host `tiny` cannot place action `classify` (Classifier), origin
`confidential`: no candidate fits. `fast` (device gpu memory 6144 quant
q4_k_m): no gpu device; `small` (device cpu memory 512 quant int8): cpu0 has
256 of 256 MiB free, needs 512. A candidate set falls back only to the roles
it names, so the placement is refused rather than moved to a device the
program did not declare; ...
```

When the miss is contention rather than a missing device, the reason names
what holds the memory: `gpu0 has 2048 of 8192 MiB free, held by fast, needs
6144`.

The refusal is fail-closed in three places:

- A host that declares no `devices` offers none. A profiled on-device role
  placed on it is refused, the same way item 119 refuses a component that
  needs a capability its host does not list
  (`docs/capability-realm-placement.md`).
- A malformed `devices` table is refused by name: an unknown or missing key, a
  device class outside `cpu`, `gpu`, `npu`, a `memory_mib` that is not a
  positive integer, an empty `quantisation` list, or a repeated device name.
  Every host's table is checked, whether or not anything is routed to it.
- There is no "any free device". A candidate set falls back only to the roles
  it names, which slice 1 already made a closed, residence-uniform, fully
  profiled set.

## What reserves nothing

Two kinds of placement take no device, and the schedule says so rather than
leaving them out:

- An `off_device` role runs off this host, so this host's devices say nothing
  about it. It is reported as `off_device, reserves nothing on this host`.
- An on-device role with no `device` clause makes no resource claim. Slice 1
  allows that only in a single-candidate arm. It is reported as `declares no
  device profile, reserves nothing`.

## The child receives it

The conductor writes each scheduled host's decision into the spec it already
hands that host's child, under `modelSchedule`: the host name, its declared
devices, and the schedule. A host that routes no model action gets no such
key, so its spec is byte for byte what it was.

The py runner (`src/revl/_process_runner.py`) does not believe the entry. Before
any component activates, it re-derives the schedule from the composition's own
files, its own components and the devices in the entry, and refuses to boot
(`BOOT REFUSED`, non-zero exit, never `UP`) when:

- the host routes a model action and the spec carries no schedule;
- the spec carries a schedule for a host that routes nothing, or for another
  host;
- the entry differs in any field from the derived schedule.

Otherwise it installs the result in `revl.model_placement`, which is what
code running in the child reads. The lookup is declared `extern pure` because
it has no observable effect: it reads a table the runner installs once, at
boot, before any component activates, and that table cannot change for the
life of the process, so the same role always gets the same answer or the same
refusal. None of the other classifications fits: nothing is acquired,
emitted or mutated.

```revl
model role fast  on_device device gpu memory 6144 quant q4_k_m
model role small on_device device cpu memory  512 quant int8

service Answer { fn classify(text: Str) -> Str }

extern pure fn model_device(role: Str) -> Str
  = @py { from revl import model_placement; return model_placement.device_for(role) }

component Classifier provides out: Answer {
  route model on classify { confidential -> fast | small }
  provide out { fn classify(text) = model_device("fast") }
}
```

- `model_placement.device_for(role)` returns the device the role is scheduled
  on in this process.
- `model_placement.claim(role, device)` returns `device` if that is the
  scheduled one.

Both raise `ModelPlacementRefused`, naming the role, for a role not scheduled
on this host (a fallback the scheduler did not pick), for a device other than
the scheduled one, and for any question in a process that was handed no
schedule:

```
ModelPlacementRefused: model role `fast` was claimed on device `gpu1`, but the
schedule for host `edge` places it on `gpu0`; ...
ModelPlacementRefused: model role `small` is not scheduled on host `edge`
(scheduled here: fast on gpu0); ...
```

Only the py runner reads the schedule. A scheduled host placed on any other
tier is refused at plan time, because a schedule its child never reads is a
decision nothing enforces. A composition that routes no model action is
unaffected on every tier.

A `revl swap` successor is scheduled for itself: the component it hosts, on
the devices the predecessor's host declared, from the files it is about to
load. A candidate that no longer fits, or a scheduled successor on a tier that
does not read the schedule, refuses the swap and leaves the running
composition untouched.

## The binding record

Each role's binding is recorded on the placement side, never in the compiler
IR. `revl audit app.rvl --placement placement.toml` prints it, and `revl run
--placement` prints the same lines before anything spawns:

```
model binding [edge]: fast on gpu0 (gpu), device gpu memory 6144 quant q4_k_m; used by First.classify confidential, Second.classify confidential
model bindings digest: 2f0c...
```

There is one row per role per host, however many actions share the role. The
digest is sha256 over `revl-model-bindings-v1`, a LF, and the canonical JSON of
every host's declared devices and binding rows. Changing one role's device,
quantisation or memory, or a host's declared devices, changes the digest. A
composition with no `route model` block has no record and prints nothing.

This digest is over what the placement declared and the scheduler decided. It
is not item 517's `placement_digest`, which the provider computes over what it
actually loaded.

## Additivity

A composition with no `route model` block schedules nothing and prints
nothing, whatever its placement says. The `devices` key is read only on the
`[processes]` form of a placement file; the `[tiers]` form synthesizes its
processes and has nowhere to declare devices, so a profiled program placed
with it is refused as placed on a host with no devices.

## What this does not do

- **It does not check hardware.** `memory_mib = 8192` is a claim in a
  configuration file, just as `device gpu memory 6144` is a claim in a
  program. The scheduler compares two declarations. What a member was actually
  loaded onto is the provider's published profile, which reaches revl only as
  the opaque `placement_digest` (`src/revl/model_profile.py`, item 538).
- **It loads only through a provision.** With `--providers`, a role bound to
  `provider = "ollama"` is loaded by its provision on the device this schedule
  chose, once per host however many model hosts route to it, and unloaded
  after the last component is gone, with the model in the residue proof
  ([providers-ollama.md](providers-ollama.md), slice S2). Every other
  provider's endpoint manages its own residency, and for those roles the
  schedule is the checked answer and nothing loads.
- **It cannot stop host code that never asks.** The provision asks
  `revl.model_placement` on every load and every call. A host body that loads
  a model itself, without a provision, is not refused.
- **It does not detect a consistent rewrite of the spec.** The child
  re-derives the schedule from the files and the devices carried in its own
  spec, so an edited decision is refused, but an edit to the devices and the
  decision together is a different, self-consistent declaration. The spec is
  written by the conductor into a `0700` placement directory; it is not signed.
- **It does not rank by cost, and a declared cost is not expressible.** "The
  small model is resident here" is a statement about a placement over time, and
  the provision now records it: every load and unload with its cost, whether the
  server already held the member before the load, and how long it was held.
  `revl run` prints that at boot and again at teardown, so the two lines differ
  ([providers-ollama.md](providers-ollama.md)). The scheduler still ranks
  candidates by the order the program wrote unless an arm writes `prefer
  resident`, which lets a candidate the host already holds beat an earlier
  written one for that arm alone: residency never reorders an arm that did not
  ask, and it is a preference an author states rather than a measurement the
  planner applies. A `model role` clause declaring a load cost is decided out of
  scope, so a declared cost stays inexpressible and item 538's measured cost is
  the answer (`docs/design/539-model-portfolio.md` §11.6).
- **A single-process run is not scheduled.** `revl run app.rvl` with no
  placement file declares no host, so there is nothing to schedule against.
