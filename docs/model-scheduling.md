# Model scheduling onto declared devices

Roadmap item 515 (issue #1189). A `model role` can declare the device it
needs, and a `route model` arm can name an ordered set of candidate roles
(`docs/design/539-model-portfolio.md`). This page covers the other half: a
placement host declares the devices it offers, and before anything spawns the
conductor picks, for every routed model action, one candidate that fits. If
no candidate fits, the placement is refused.

The code is `src/revl/model_schedule.py`, called from `run_placement` in
`src/revl/placement.py`. The tests are `tests/test_model_schedule_515.py`.

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
  host declares them.
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
- **It does not load or unload anything.** The schedule is a plan-time
  decision that the conductor prints and refuses on. Nothing passes it to the
  child processes yet. The provision keyed by role, with one load, one unload
  and `no_residue` at teardown for N consumers, is slice S2 and is not built.
- **It has no load cost and no residency over time.** "The small model is
  resident here" is a statement about a placement over time; slice S3 owns it.
  The scheduler ranks candidates by the order the program wrote, not by cost.
- **A single-process run is not scheduled.** `revl run app.rvl` with no
  placement file declares no host, so there is nothing to schedule against.
