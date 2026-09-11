# Component leases — the composition as a multi-agent workspace

Roadmap item 61. Module: `src/revl/mcp/leases.py`. Verb: `revl_lease`.

## The problem leases solve

The MCP session (`docs/mcp-bridge.md`) turns a running composition into a
workspace an agent drives: generate → check → admit → swap. Item 55
(`docs/operator-capabilities.md`) answers *who may drive this session* — it
scopes each management verb to an operator's grants.

Neither answers the question that appears the instant **two** agents drive the
same running system:

> Who is allowed to replace `UserCache` **right now**?

Both agents may hold the `swap` grant for `UserCache`. Nothing stops agent A
from hot-swapping a new `UserCache` out from under agent B while B is halfway
through iterating on its own candidate. The swap races; the loser silently
loses work. The *running* component kept serving the whole time — that was
never in doubt — but the **right to replace it** was contended, and no one
arbitrated.

A **lease** is that arbitration.

## What a lease is (and is not)

A lease is an **operator-scoped, TTL-bound claim on a component _name_**:

> "agent B is iterating on `UserCache` until 14:32."

- **Operator-scoped** — the holder is item 55's operator identity (the
  session's bound operator token), no new notion of identity. A session with no
  operator profile claims under a single default holder (`operator`); the
  distinctions below only bite once profiles bind *distinct* operator tokens,
  which is exactly the multi-agent case leases are for.
- **TTL-bound** — a lease carries a wall-clock expiry. A lease with no renewal
  expires on its own, so a crashed or walked-away agent never wedges the
  workspace. Every read prunes expired leases first, so expiry needs no
  background timer.
- **A claim on a name, not a lock on the component.** This is the load-bearing
  distinction. The running component keeps serving **every call** throughout.
  A lease governs only *who may replace it* — the management plane, never the
  data plane.

## The three registers

A lease escalates through three registers. The first two are always on; the
third is opt-in per policy.

### 1. Surfaced — `revl_state`

Every active lease shows in the session state (holder, component, expiry), so
an agent can survey the workspace before it acts:

```json
"leases": [
  {"component": "UserCache", "holder": "bob",
   "acquired": 1700000000.0, "expiry": 1700000320.0, "expiresInSeconds": 118.4}
]
```

Active leases are visible even with nothing loaded — an agent can claim intent
on a name before it boots.

A lease that was **not** claimed in this session — one re-seated from a restored
snapshot — adds `"verified": false` to the record. It still fences every other
operator; it does not exempt its own holder. See *Self-operator is always
allowed* below.

### 2. Advisory (default) — `revl_plan` / `revl_swap`

A plan or swap that would replace a component **another operator** leases is
*warned*, but proceeds:

```json
"leaseWarnings": [
  {"component": "UserCache", "leasedBy": "bob", "expiresInSeconds": 118.4,
   "message": "`UserCache` is leased by `bob` for another 118.4s — your swap
               will race their iteration (component leases, item 61; advisory
               unless policy enforces leases)"}
]
```

This is coordination without coercion. The warning names the race; the agent
decides. `revl_plan` derives the warned set from the candidate — a swap that
only touches `UserCache` warns only about `UserCache`'s lease, never about a
lease on a component it leaves alone.

### 3. Enforced (where policy says so) — admission refusal

Under a boundary policy (item 33, `docs/boundary-policy.md`) that declares
leases enforced, that same swap is **refused** at admission:

```
# in the boundary policy file
leases enforced
```

or, in JSON:

```json
{ "leases": { "enforced": true } }
```

Bind the policy to the served session with `revl mcp serve --policy FILE`. Now
a swap that would replace a component another operator leases is refused with
the policy-style why-trace every other refusal here carries — and the running
composition is left **untouched**:

```json
{
  "ok": false, "swapped": false, "authorized": false,
  "note": "the running composition is untouched — a lease held by another
           operator, enforced by policy, refused this replacement",
  "lease": {"component": "UserCache", "heldBy": "bob", "operator": "alice"},
  "why": {"kind": "component-lease", "subject": "alice",
          "path": ["alice", "UserCache"]}
}
```

The refusal is **all-or-nothing**, like admission: the first target held by
another operator refuses the whole swap.

Enforcement covers **every path that swaps a component candidate**, not just
`revl_swap`: `revl_ship --apply` reaches it through the swap handler,
`revl_repair`'s remediation step is checked here before the loop runs, and
`revl_edit` — whose edits compile into the same kind of candidate swap — is
checked against this gate and the quarantine gate before its swap runs, rather
than only against admission. A gate that only `revl_swap` walks is not a gate;
it is a gap in the shape of the tool an agent reaches for when the front door
is closed. And
when the swap's **targets cannot be derived** — a candidate that will not
compile — enforcement fails closed and checks the swap against *every* active
lease, rather than against none of them. A swap that cannot be scoped is
exactly the swap a lease exists to stop.

Generation replay is outside that claim: `revl_undo` and `revl_rollback`
re-admit a retained generation and reach `Session.swap` with neither this check
nor the quarantine gate, under the operator's `undo` authority instead.

The advisory/enforced split is the same shape as the rest of the gate:
advisory by default (any operator with the `swap` grant can still act), enforced
where the composition's owner has declared, in policy, that leases are binding.

## Self-operator is always allowed

A lease never blocks its **own** holder. Alice may freely renew, release, plan
against, and swap the components she holds — the lease protects her iteration
*from others*, it does not fence her out of her own work. Enforcement and
advisory both compare the swap's target leases against the acting operator and
skip the ones that operator holds.

That exemption is earned by **claiming the name in this session**, not by the
name appearing in a document. `revl_restore` re-seats the leases it finds, and
a restored lease is indistinguishable, in the snapshot, from one the restoring
operator typed: the document is caller-supplied input, exactly like the source
text next to it. So a re-seated lease carries the fence and not the exemption —
it still stops every other operator, but it will not let the operator its
`holder` field names through the gate until that operator claims the name with
`revl_lease`. Otherwise anyone who can call `revl_restore` could mint a lease in
the one name the gate exempts and walk past enforcement in the same breath.
`revl_state` marks these leases, and the refusal says which case you are in.

## The verb — `revl_lease`

One verb, three actions:

| action    | effect                                                             |
|-----------|--------------------------------------------------------------------|
| `claim`   | claim a name (or, by the holder, extend it); refused if another operator holds it live |
| `renew`   | extend a lease you hold; refused if there is none, or another holds it |
| `release` | drop a lease you hold; releasing an absent lease is a quiet no-op   |

`ttl` (seconds) sets the duration; the default is 300s. Claiming your own live
lease is a renewal that keeps the original `acquired` time. A name another
operator holds live is **never stealable** — wait for its TTL to elapse, or
have the holder release it.

A TTL may not exceed `MAX_TTL` (86400s, one day); a longer claim is refused
rather than truncated, so the caller knows its lease is not what it asked for.
The bound exists because the TTL is what makes "a walked-away agent never wedges
the workspace" true: a lease long enough to outlive the workspace is a fence
instead of an iteration window. See Persistence below for why the same bound
applies to a lease arriving from a document.

## Visible in the causal trace (item 27)

Every claim, renewal, release, and TTL expiry is stamped as a `lease`-channel
trace event (holder, component, expiry, timestamp). When a composition is
loaded, those events ride the session's causal trace beside the lifecycle
story, so *"who held what lease when"* is one query over the same trace as
everything else (`revl_state`'s `trace`, `revl_history_lifetime`).

## Persistence (item 15)

Leases are wall-clock claims, so a snapshot (`docs/persistence.md`) records the
active set in its meta, and `revl_restore` re-seats only the leases still live
at restore time — any whose TTL elapsed while the snapshot sat are silently
dropped. A claim does not come back from the dead across a restart.

The re-seated lease is **bounded** the same way a claimed one is: its `expiry`
is clamped to `MAX_TTL` from restore time. `expiry` comes out of the same
caller-supplied document as `holder`, so without the clamp a document could
re-seat a fence over a name no operator token matches — no one is exempt from
it, and `release` is holder-checked, so no one can lift it either — and it would
never expire on its own. That is a veto over `revl_swap`/`revl_repair` for every
real operator that only a restart clears: the wedge the TTL exists to rule out.
Clamped, the worst a snapshot can install is a delay that ends on the schedule a
real claim could have asked for. A non-finite `expiry` is either clamped
(`Infinity`) or dropped (`NaN`, which is not an instant at all).

## Where the code lives

- `src/revl/mcp/leases.py` — the `Lease`/`LeaseBook` model (pure bookkeeping
  over the clock), the holder-identity resolution, the advisory (`advise` /
  `advise_plan`) and enforcement (`check_swap`) decisions. Reuses item 55's
  target derivation (`operator._targets`) read-only to scope a swap to the
  components it replaces.
- `src/revl/mcp/session.py` — the `LeaseBook` lives on the session; `state()`
  surfaces the active set.
- `src/revl/mcp/server.py` — the `revl_lease` verb, the advisory hook in
  `revl_plan`, and the enforcement hook in `revl_swap`.
- `src/revl/policy.py` — the `leases enforced` flag (item 33) that promotes the
  advisory to a refusal.
- `src/revl/mcp/persist.py` — snapshot/restore reflection.
