# Design: witnessed network effects (item 254)

> **Superseded in part.** The "Revision (adversarial review 2026-09-01)"
> section immediately below is authoritative. It recasts the whole feature from
> a **proof-surface witnessed network write** to a **compensate-grade network
> effect**. Sections §1 through §7 predate that review; wherever they claim a
> witnessed network WRITE, state-restoration, `noResidue`, a proof surface, or a
> G5 waiver, those claims **do not hold** and are superseded by the Revision
> section. They are retained below for context and for the parts the revision
> keeps (the per-endpoint evidence discipline, the verb gate, the network cap
> scope). §7's Slice 1 has been rewritten in place to the compensate-grade slice.

---

## Revision (adversarial review 2026-09-01)

An independent adversarial review found a **new CRITICAL that invalidates the
central premise** of the pre-revision design (that a network write can be a
proof-surface `witnessed` effect), plus two HIGHs. This section folds all three
in and re-slices. It is authoritative over the sections below.

### CRITICAL - a network reversal cannot be a proof-surface witness; the landed rule-3 check refuses it

The pre-revision design (§1.3) generates a `witnessed[net.*]` extern whose
`undo` is a reversal `PUT`, and asks G5 to grant a "waiver" (§5) so that
emitting reversal can run at teardown. **Landed code refuses this outright.**

`_check_witnessed_inverse` (`src/revl/lower.py:2153`, called at `:3095`)
statically walks a witnessed extern's `undo` and raises `code="G5",
category="witnessed"` if the inverse calls any `emission`- or
`witnessed`-classified extern, or a plain `fn` that transitively reaches one
(the `emitting_fns` emission-reach fixed point over the call graph). This is
rule 3 of item 243: a witnessed inverse must be a **host-local restore**, so
only `pure`/`acquire` callees are admissible.

`import_openapi.py` classifies **every** `PUT`/`DELETE` as `emission idempotent
fn` (`:19` intro, `_IDEMPOTENT_EMISSION_METHODS` at `:87`, the
`emission and method in ...` decision at `:705`, and the emitted `extern
emission fn` at `:1021`). So the generated reversal-`PUT` extern **is** an
`emission`, and the witnessed-net declaration is **refused at lower time**.
**Slice 1 as originally drafted cannot compile.**

All three ways out are closed by landed code:

1. Classify the reversal `emission` (the honest classification) -> refused
   directly by rule 3 (`lower.py:2153`).
2. Classify it `pure`/`acquire` to pass rule 3 -> the net crossing then vanishes
   from the G8 audit surface, from item-33 boundary policy, and from the
   emission-reach fixed point, contradicting this design's own §5/G8
   enumeration promise. Mutually exclusive with (1): you cannot both audit the
   crossing and hide it from the checker.
3. Name the `PUT` as its own `undo` -> refused as "itself witnessed, infinite
   regress" (`lower.py:2189-2194`).

**Why compensate, not witnessed.** By revl's own taxonomy (item 247,
`docs/design/247-compensate.md`), a network reversal is **fallible** (5xx,
timeout) and **non-local**. That is the definition of a `compensate`, not an
`undo`/witness. The item-247 table is explicit: a `compensate` **may emit** in
teardown (that is the point), is **best-effort**, **may fail** (feeds item 246),
runs on abort **after** inverse replay, **never** on clean unload, stays
irreversible, and lands on the **AUDIT** surface (intention, G8) - never the
**PROOF** surface (guarantee, G4/G7, `noResidue`). A witness's inverse must be
host-local and infallible; a network reversal cannot meet that bar and meets the
compensate bar exactly.

### The recast

Recast item 254 as **compensate-grade network effects**, not proof-surface
witnessed network writes. Per-endpoint declaration classifies:

- a `PUT`-with-readable-preimage,
- a `DELETE`-with-documented-recreate,
- a `POST`-with-documented-delete,

as **`compensate`-grade** (item 247). The reversal emits **legally** through the
compensate slot at teardown (a `compensate` may emit), lands on the **audit**
surface, and **never** contributes to a `noResidue`/witness proof.

- **Drop** every "witnessed network WRITE" proof-surface claim and the
  **G5-waiver framing** of §5. The waiver is not this design's to grant; the
  compiler **refuses** it (`lower.py:2153`). §5's line "the 'host-local inverse'
  property of item 247's table is explicitly waived for `witnessed[net.*]`" is
  withdrawn.
- **Keep** the per-endpoint evidence discipline: the RFC 9110 idempotency claim,
  the `x-revl-*` annotations, and the claim-vs-proof header. The classification
  **ceiling is `compensate`, not `witnessed`.**
- The reason, cited plainly in the generated header and here: `lower.py:2153`
  refuses an emitting inverse, `import_openapi.py:19` makes every PUT/DELETE an
  emission, and item 247 is the correct home for a fallible non-local reversal.

### Future / OPEN (explicitly NOT Slice 1) - a genuine proof-surface network witness

A real proof-surface network witness is a **separate language-design item**, not
something `import openapi` can deliver. Sketch of what it would require:

- A **new language construct** (working name **`witnessed-with-observable-inverse`**)
  whose inverse is **accounted on the G8 boundary surface** as an outbound
  crossing yet is **exempt from the rule-3 emission-in-teardown refusal**
  (`lower.py:2153`) that today refuses any emitting inverse. This is a new
  taxonomy slot, not a waiver bolted onto `witnessed`.
- Its own **fallibility story** tied to item 243 rule 6: an inverse that can 5xx
  or time out must feed the **item-246 restore-residue prompt**, never silently
  claim `noResidue`.

Slice 1 does **not** deliver this and must not pretend to. It ships the
compensate-grade classification only.

### HIGH 1 - If-Match / TOCTOU under the recast

Under the recast the reversal is compensate-grade, best-effort, on the audit
surface, and no longer claims `noResidue`, so the TOCTOU is **less
catastrophic** than the pre-revision framing implied. It is not gone: a
compensate that PUTs the captured preimage under a **concurrent writer**
clobbers the intervening write.

Requirement for Slice 1: recommend **`If-Match`/`ETag`** on the reversal `PUT`,
and **either** refuse a compensate-reversal on an endpoint that exposes **no
version/ETag token**, **or** mark it explicitly **best-effort-may-clobber** on
the audit surface (the header states which). A refusal path plus an explicit
may-clobber path, never a silent clobber.

### HIGH 2 - the item-250 mixed cap-scope predicate

Pin the quantifier precisely. An inverse is **enumerated-not-run** by the item
250 fork rewind **iff its cap scope contains ANY non-host-confined cap** (net,
IPC, outbound). A single net cap **taints the whole inverse**:

```
# fragment - the corrected 250 rewind predicate (not revl source)
enumerated_not_run(inverse)  <=>  exists cap in scope(inverse): cap not in host_confined
```

So a mixed-scope reversal `compensate[fs, net.x]` (the compensate equivalent of
the pre-revision `witnessed[fs, net.x]`) is **never speculatively fired** during
a fork rewind, because its `net.x` component taints it, even though its `fs`
component alone would have been host-confined and run. Slice 1 pins **both** the
pure-net case **and** this mixed `[fs, net.x]` case as tests, so a
mixed-scope reversal is never speculatively fired mid-fork.

### Verb gate (validated, kept)

The RFC-verb gate (§6 attack 3) is **kept unchanged** under the recast:
`x-revl-*` promotion is honored **only on `PUT`/`DELETE`** and is a **hard
error** on `POST`/`PATCH`, so the annotation can only narrow within
already-idempotent verbs, never invent idempotence. The honest open point -
**idempotence is not reversibility** - remains OPEN behind the item-37 verified
round-trip. No change beyond noting it now lands under the compensate recast.

### Re-sliced Slice 1 (compensate-grade, py tier, NO proof-surface claim)

1. **`import openapi`**: the `x-revl-compensate` / `x-revl-preimage` /
   `x-revl-undo` annotations and their `--compensate`/`--preimage`/`--undo`
   engineer equivalents, honored **only on `PUT`** (verb gate), attaching the
   **item-247 `compensate` slot** to the operation's `emission` extern with a
   **network cap scope**; **hard refusal on `POST`/`PATCH`**; the claim-vs-proof
   header block stating **compensate-grade / audit-surface**, the reversal-never-
   un-observes sentence, and either **`If-Match` required** or
   **best-effort-may-clobber** (HIGH 1).
2. The generated shape reuses item 247's **landed compensate slot** (extern-level
   `emission ... compensate <expr>`, `parser.py:1022-1028`; legality on an
   `emission` extern, `lower.py:1577-1602`; py emit `yield lambda:`,
   `backends/python/emit.py:939-942`). The reversal fires in **Phase-2 on abort,
   after inverse replay**, on the audit surface. **No `HttpWitness`, no
   `noResidue`, no proof surface.**
3. The **network cap scope** on the IR node, plus the **corrected item-250
   predicate** (HIGH 2) pinned as tests - the pure-net case **and** the mixed
   `[fs, net.x]` taint case - so a net-tainted compensate-reversal is
   enumerated-not-run by a fork rewind even when 250 itself is not in this slice.
4. py-tier host-body stub + one fixture endpoint round-tripped through
   `compile_files` (as `test_import_openapi.py` does), plus an
   **audit-enumeration** test: the reversal shows on the G8 / `erase_report`
   surface as its own outbound crossing, tagged **compensated, not undone**.

Explicitly **not** in Slice 1: any `noResidue`/proof-surface claim; the
`witnessed[net.*]` classification; the `witnessed-with-observable-inverse` future
construct; the `DELETE`/`POST` compensate forms (Slice 2); other tiers (Slice 3);
the item-37 verified-idempotent upgrade.

---

Status: design proposed. No implementation. Grounds on the landed
witnessed-externs machinery (item 243, `docs/design/243-witnessed-externs.md`),
the idempotency-evidence IR (item 44, `docs/delivery-semantics.md`), the
OpenAPI importer (`docs/import-openapi.md`, `src/revl/import_openapi.py`), and
the §4.1 / §4.2 concessions in `docs/replay.md`. It also states the contract
that item 250 (session branching) depends on, per that item's fork-rewind
review.

## The one thing to get right

Item 243 moved the reversible/irreversible boundary for the filesystem by
engineering a witness: an `rm` returns a durable `FsWitness`, the accumulator
auto-registers the declared `undo restore(result)`, and an abort replays it to
put the pre-state back. The move works for HTTP **only where the endpoint's
documented semantics let a real inverse exist**, and it works endpoint by
endpoint, never wholesale:

| HTTP shape | evidence that permits it | classification |
|---|---|---|
| `PUT x` with a readable `GET x` preimage | `PUT` idempotent, RFC 9110 §9.2.2; the resource is fully replaceable | **witnessed write** - undo = `PUT` the preimage back |
| `DELETE x` against a recreate-capable endpoint | `DELETE` idempotent; a documented create rebuilds `x` | **witnessed remove** - undo = recreate `x` from the captured body |
| `POST` to a resource with a documented delete | no inverse, but an offsetting crossing exists | **`compensate`-grade** (item 247), not witnessed |
| everything else | none | **honest `emission`** |

The load-bearing distinction is between the first two rows and the third. A
witnessed write **restores server state** (the inverse `PUT` sets the resource
back to the preimage byte-for-byte, and idempotence means the last writer
wins). A `compensate` **does not** restore state; it issues a second, offsetting
crossing and the report says so (§4.2, `docs/replay.md`). The whole risk of this
item is letting a `POST`+`DELETE` masquerade as a witness, or letting a `PUT`
that the server does not actually treat as idempotent claim state-restoration it
cannot deliver. The evidence rule below is the guard, and it is item 243's rule
verbatim: **witnessed status is declared per endpoint and flows from documented
semantics, never inferred from optimism.**

There is a second thing this item must be loud about, and it is the reason the
module exists rather than being a one-line reuse of 243: **the §4.1 caveat is
strictly stronger over a network, and a network witnessed inverse is not
host-local.** Both points get their own sections (§3, §5).

---

## 1. The per-endpoint declaration surface

The declaration lives in `import openapi`, which already carries the evidence
signal (`docs/import-openapi.md` §1). Nothing new is declared by hand in the
common path - the importer emits it - and every classification is echoed into
the generated header next to the operation it governs.

### 1.1 What the importer already does (item 44, landed)

For `PUT` and `DELETE` the importer today writes `emission idempotent fn`, on
the RFC 9110 §9.2.2 evidence, with the claim-vs-proof header already stating
the three things that matter - that safe-by-spec is a claim not a proof, that
*idempotent is not reversible*, and that idempotency rides along as a delivery
claim (`docs/import-openapi.md` §1, `docs/delivery-semantics.md`). Item 254 does
**not** weaken any of that. It adds a strictly narrower classification on top of
it, reachable only with **more** evidence than idempotency alone.

### 1.2 The new evidence: a declared preimage/recreate route

Idempotency is necessary but not sufficient. A witnessed write also needs a
**preimage source** - a way to capture the pre-state and a way to put it back
through the same endpoint. That is a second, separate claim, and it is declared
with new `x-revl-*` annotations on the operation, mirroring the existing
`x-revl-emission` override family:

```
# sketch - OpenAPI operation annotations (not revl source)
put:
  operationId: setConfig
  x-revl-witnessed: true         # author claims a real inverse exists
  x-revl-preimage: getConfig     # the GET operation that reads the preimage
  x-revl-undo: setConfig         # the operation that writes the preimage back
```

- `x-revl-preimage` names a **safe** operation (its own classification must be
  safe-by-spec or `--pure`-claimed) whose response type is assignable to the
  witnessed operation's request body. For a `PUT`, `x-revl-undo` is the `PUT`
  itself; for a `DELETE`, `x-revl-undo` names the documented **create**
  operation and `x-revl-preimage` names the `GET` whose body the create
  consumes.
- Absent these annotations a `PUT`/`DELETE` stays exactly what item 44 makes it:
  `emission idempotent fn`. The importer never promotes to witnessed on the verb
  alone. Optimism is structurally excluded because the promotion requires a
  *second* author claim naming *concrete other operations*, not a mood.
- The importing engineer's out-of-band equivalents (`--witnessed <op>`,
  `--preimage <op>`, `--undo <op>`) exist for the same reason `--pure`/`--emission`
  do, and follow the same asymmetry (§2): a witnessed claim can be *revoked* by
  anyone (`x-revl-witnessed: false`, `--emission`), but *asserting* it takes the
  full route declaration from an author or engineer.

### 1.3 What the importer generates

A witnessed operation lowers to the item-243 surface - the fourth extern
classification, capability-scoped, with a WAL-serializable witness and an
auto-registered declared inverse:

```revl
// sketch - generated by `revl import openapi`; not standalone-compiling
// PUT /config/{id} - declared witnessed by x-revl-witnessed (author claim)
//   preimage: GET /config/{id}   undo: PUT /config/{id}
//   `PUT` is idempotent by RFC 9110 §9.2.2: the author's claim, not a proof
//   witnessed inverse crosses the NETWORK at teardown - see header §4.1
witnessed[net.config_api] fn http_set_config(id: Str, body: Config)
    -> Result[HttpWitness, HttpError]
  undo http_restore_config(result)
  = @py { /* GET preimage, PUT body, capture pre-bytes into the witness */ }
```

The witness is durable data, not a live handle (item 243 rule 4): it carries the
resolved URL, the captured preimage bytes (or a content-addressed ref to them),
the method, and the headers needed to re-issue the reversal. `recovery.py`
reconstructs it after a crash as a named call with captured arguments, exactly
like an `FsWitness` - nothing about the recovery path is network-specific except
that the reconstructed call reaches the wire (§5).

### 1.4 Preimage capture is inside the forward effect, on the Ok path only

The `GET` preimage is captured **inside** `http_set_config`, before it issues the
`PUT`, and the witness is returned only on `Ok`. This is deliberate:

- It reuses item 243's Ok-conditional registration unchanged (`undo` binds the
  `Ok` payload as `result`; a failed forward call registers no inverse).
- It keeps the preimage and the mutation in **one** boundary crossing pair from
  the accumulator's point of view, so the TOCTOU window (§6, attack 1) is
  exactly the GET→PUT gap and nothing wider.
- A `GET` that itself fails (endpoint down, 404 on a resource being created)
  makes the forward call return `Err` with no witness: a `PUT` whose preimage
  could not be read is **not** witnessed, it is a bare emission for that call,
  and it must say so rather than register an inverse it cannot honor.

---

## 2. The evidence rule (unchanged from the import family)

> Witnessed status flows only from documented semantics, and the generated
> header states claim vs proof.

This is item 243's rule and `import openapi`'s §1 rule, applied to a narrower
classification. Three claims stack, each stated in the header next to the
operation, none of them checked by this compiler:

1. **safe-by-spec** for the preimage `GET` (RFC 9110 §9.2.1) - the author's word;
2. **idempotent-by-spec** for the `PUT`/`DELETE` (RFC 9110 §9.2.2) - the author's
   word, already the item-44 claim;
3. **a real inverse exists** - `x-revl-witnessed` + the preimage/undo route - a
   *new* author claim, the strongest and the one most easily wrong.

The header must reproduce `import openapi`'s existing sentence structure so the
reader sees the same shape they already trust:

```
// `PUT /config/{id}` is declared WITNESSED by x-revl-witnessed (author claim,
//   not a property this compiler checked): its inverse `PUT /config/{id}` with
//   the GET preimage is claimed to restore server state. This compiler cannot
//   verify the endpoint is truly idempotent, that the GET reads exactly what
//   the PUT wrote, or that no other writer intervenes. Read the host body and
//   the endpoint's docs before trusting the reversal. Reversal restores SERVER
//   STATE only, best-effort; it never un-observes the crossing (§4.1).
```

The asymmetry from `import openapi` holds and points the same way G4 points:
**strengthening (to emission) takes one voice; weakening (to witnessed) takes
unanimity.** `x-revl-emission: true` or `--emission <op>` demotes a witnessed
operation to a bare emission from any single source. Promotion to witnessed
requires the author's positive route declaration and is refused if any
participating operation's own classification contradicts it (a preimage `GET`
that some override marked `emission` cannot serve as a preimage - an emission
read is not a safe read).

---

## 3. The §4.1 caveat, stronger over a network

`docs/replay.md` §4.1 says an inverse is not an undo of the world: it restores
what the application calls equivalent, not the world's observation of it. §4.2
adds that a compensation is a second crossing, never an un-crossing, and that
anything downstream that observed the first - "a trigger, a replica, a webhook, a
human - has already observed it."

Over a network this stops being a footnote and becomes the common case. The
module header and this design state it without softening:

- **A witnessed network write restores server state, best-effort. It never
  un-observes.** The reversal `PUT` puts the resource back to the preimage; it
  does not unsend what a webhook subscriber, a change-data-capture stream, a
  cache, a read replica, or a human already saw. On the filesystem the analogous
  observers are rare (another process that `stat`ed the file mid-transaction);
  on a network **observers are the default**, because publishing changes to
  subscribers is what these endpoints are for.
- **So a witnessed network write is honest about a smaller claim than an fs
  witness.** The claim it makes is: *the resource's server-side state returns to
  its pre-value, idempotently.* The claim it does **not** make is: *no observer
  retained the intermediate value.* The header states both - the positive claim
  and the explicit non-claim - because the reader who ports fs intuitions to the
  wire will otherwise assume the stronger one.
- **The reversal is itself a crossing and is enumerated as one.** Unlike an fs
  inverse (host-local, unobserved), the reversal `PUT` crosses the boundary
  again and is itself observable. It is recorded on the G8 surface as an outbound
  crossing at teardown (§5), printed, never pretended away - the same discipline
  §4.2 already applies to a compensation, applied here to a witnessed inverse
  because over the network it shares the compensation's observability even though
  it (unlike a compensation) does restore state.

The one-sentence version, which belongs verbatim in the generated header: **a
rewound `PUT` does not unsend what a webhook subscriber already saw.**

---

## 4. Capability scope: a network cap, not host-confined (item 250 contract)

A witnessed network operation is capability-scoped like any boundary crossing
(`witnessed[net.config_api]`), joining the same authority namespace and
policy/audit accounting as emissions (item 243 rule; `parser.py:497-502`,
`emission_analysis.py`). The scope token is the network destination, realm-style
dotted per item 343 (`net.config_api`, or the resource-bound `gwsend(host=...)`
shape item 251 records), and it is carried onto the IR node and the audit
surface.

**The scope is a NETWORK cap. It is not host-confined. This is load-bearing for
item 250 and must not be relaxed.**

Item 250 (session branching) forks a session at step *k* by replaying witnessed
inverses back to *k* so the workspace is *actually in* the step-*k* state. Its
fork-rewind review raised a CRITICAL: the rewind runs witnessed `KIND_EFFECT`
inverses, and a network-witnessed inverse is an **outbound crossing** - running
it during an exploratory rewind would `PUT` to a remote endpoint
*speculatively*, producing exactly the external residue that deferred-class
actions (item 245) exist to prevent while N branches are explored. The item 250
revision therefore keys the non-emitting rewind on the inverse's **capability
scope**: it runs only **host-confined** inverses during a fork rewind, and
**enumerates-not-runs** any inverse whose scope crosses the network.

This item's obligation is to make that key true and unambiguous:

- A network-witnessed inverse **carries a network cap scope** (`witnessed[net.*]`),
  never a host-confined one. There is no spelling of a witnessed network write
  whose inverse looks host-confined to the 250 rewind. An fs witness
  (`witnessed[fs]`) is host-confined and runs on a fork rewind; a network witness
  is not and does not.
- A network-witnessed inverse is therefore **enumerated, not run, on a 250 fork
  rewind** - listed on the branch's rewind report as an outbound crossing that
  the branch chose not to re-issue, so the reader knows the branched workspace's
  *remote* state was **not** rewound (only local state was), which is honest and
  is the §3 caveat again.
- It is `compensate`/recovery-run **only at genuine teardown** - a real abort or
  clean-unload discharge of the owning frame, or `revl recover` after a crash  - 
  **never speculatively**. Genuine teardown is the one context where re-crossing
  the wire to restore server state is the intended, accounted-for action.

This is the CRITICAL cross-item interaction; if a future change let a network
inverse present a host-confined scope, item 250 would silently start issuing
speculative remote `PUT`s during branch exploration. The invariant to pin in
tests: **`witnessed[net.*]` scope ⟹ enumerated-not-run by the 250 rewind
predicate.**

---

## 5. G-invariant interaction

### G5 - teardown cannot register effects (and the emit-in-teardown tension)

G5 holds "by construction: `undo` bodies are pure expressions, so there is no
syntactic slot for an effect during teardown" (`docs/contract-errata.md:552`).
That construction is unchanged here: the network inverse's body is a
pure-expression call to the declared undo extern, and teardown registers no
*new* effect. **So G5, as stated (no *registration* of effects during
teardown), is not violated.**

But G5's sibling intuition - the one written into item 247's table as
"`witnessed`: may emit in teardown = **no** (host-local inverse)" - **is not true
for a network witness**, and the design must say so rather than quietly rely on
the fs framing:

- An fs witnessed inverse is host-local: teardown restores a file, crossing
  nothing. Item 247's table row is correct for item 243/244.
- A **network** witnessed inverse **does cross the boundary at teardown**. The
  reversal `PUT` is a real outbound crossing.

The reconciliation, stated plainly: G5 forbids *registering* effects during
teardown, and that still holds - the inverse was registered at the forward call
site (Ok-conditional, item 243), and teardown merely *replays a pre-registered,
pre-accounted-for* inverse. The crossing that replay performs is not a new,
unbudgeted effect; it is the reversal that the witness declaration reserved and
that the G8 surface enumerated at registration time. What changes versus the fs
case is only that this replay is *observable*, and that is handled not by G5 but
by the §3 caveat and by enumerating the reversal on the G8 boundary surface as
an outbound crossing-at-teardown. In short: **G5 (registration) is preserved;
the "host-local inverse" property of item 247's table is explicitly waived for
`witnessed[net.*]`, and the waiver is paid for by G8 enumeration and the §3
honesty, not hidden.** This is the honest reason a network witness sits closer to
`compensate` on the observability axis while staying a true witness on the
state-restoration axis.

### G7 - LIFO-complete derived teardown

Unchanged and inherited from item 243's Slice 2a/2 contract. A network witnessed
inverse joins the same LIFO disposer stack / deferred-transactional park as an fs
witness; a mid-session network witnessed effect replays newest-first across the
frame on abort, identical to item 243 point 5. The LIFO discipline matters for
the same reason: two `PUT`s to the **same** resource must reverse newest-first,
or the older reversal restores an intermediate value the newer one then clobbers
back to the wrong state. Because a witnessed `PUT`(preimage) is idempotent and
total, a FIFO drain would let the oldest reversal win and destroy the true
pre-state while the report still claims `noResidue` - the exact failure item 243
pins for fs, now on the wire. The network case must carry item 243's
`test_witnessed_abort_lifo` analog.

### G8 - enumerable boundary

The witnessed network capability is on the same audit surface as emissions
(item 243). Additionally, per §3/§5, the **reversal crossing at teardown is
itself enumerated** as an outbound event, so a session's boundary record shows
both the forward witnessed write and, if it aborted, the reversal `PUT` - never a
silent un-crossing.

---

## 6. Adversarial self-review

Every prior design review in this family surfaced a CRITICAL. Mine is **attack
3** below; I am stating it as CRITICAL-and-mitigated rather than hoping a
reviewer misses it.

**Attack 1 - GET-preimage races another writer (TOCTOU).** Between the
preimage `GET` and the `PUT`, a third party writes the resource. The witness now
holds a preimage that was never the immediate pre-state of *our* write, so the
reversal restores a value that skips the intervening writer's change.
*Mitigation (partial, honest):* where the endpoint supports it, capture the
preimage's `ETag`/version and issue the reversal `PUT` with `If-Match`, so a
reversal that would clobber an intervening write **fails loudly** and feeds the
item 246 restore-residue prompt (item 243 rule 6) rather than silently
destroying data. Where the endpoint offers no concurrency token, this is an
**OPEN** limitation the header must state: "witnessed reversal assumes this
resource is not concurrently written; it has no way to detect a racing writer."
This is strictly weaker than the fs witness (a rename-to-garbage has no such
race) and the header must not pretend otherwise. TOCTOU is why item 243 rejected
`revertible` as a name; the same honesty applies here.

**Attack 2 - DELETE against a soft-deleting endpoint (recreate is not a true
inverse).** `DELETE x` is declared a witnessed remove with a recreate undo, but
the server only *soft*-deletes (tombstones, changes an id, resets a
server-assigned field, drops audit history). The "recreate" produces a resource
that is observably not the original. *Mitigation:* witnessed-remove promotion
requires the author to declare **both** the recreate operation **and** that the
`GET` preimage captures the full recreatable body; if the recreate's request
schema cannot carry a field the original had (e.g. a server-assigned immutable
id), the importer can detect the schema gap and **refuse the witnessed
promotion**, leaving it `compensate`-grade. What it cannot detect is a
*semantic* soft-delete whose schemas look total. That residue is **OPEN** and
stated in the header: a witnessed remove restores a resource that the API author
claims is equivalent, on the API author's notion of equivalence (§4.1 verbatim).
Recommendation: keep witnessed-remove **out of Slice 1** so this weaker
guarantee does not ship before the write case is solid.

**Attack 3 (CRITICAL) - an endpoint declared witnessed via `x-revl-*` that is
not actually idempotent (optimism leaking in through the annotation).** The
whole evidence rule rests on documented semantics, but `x-revl-witnessed: true`
is written by a human who can be wrong or optimistic, and unlike safe/idempotent
(which at least track a normative RFC verb) the witnessed route is a bespoke
claim with no verb behind it. A `PUT` that the server implements as
*append/merge* rather than *replace* is not idempotent, and `PUT`(preimage)
restores nothing - it appends the old value on top of the new. The optimism the
whole family forbids re-enters through the annotation. *Mitigation:* (a) the
promotion is **gated on the RFC verb first** - `x-revl-witnessed` is honored only
on a `PUT` or `DELETE` (verbs the RFC defines idempotent) and is a **hard error**
on a `POST`/`PATCH`, so the annotation can only *narrow* within already-idempotent
verbs, never invent idempotence; (b) the header states, in the claim-vs-proof
block, that witnessed rests on *three stacked unverified claims* and names the
third as the weakest; (c) the item 37 *verified-idempotent* / verified-effect
upgrade path (`docs/delivery-semantics.md`, `docs/verified-effect.md`) is the
real closure - a recorded `PUT`;`GET`;compare round-trip against the live/replayed
endpoint promotes the claim to a *test the author did not write*, and this design
recommends the witnessed promotion be the **first** classification to require
that verified upgrade before it is trusted in a production realm. Until then the
mitigation is honesty + the verb gate, and the residual risk is **explicitly
OPEN** and headed. This is the CRITICAL because it is the one place optimism can
re-enter a family whose entire thesis is that it does not.

**Attack 4 - the observation caveat under a webhook subscriber.** A witnessed
`PUT` fires a webhook to N subscribers; the session aborts; the reversal `PUT`
fires a *second* webhook. Downstream a subscriber now sees value→newvalue→value,
having acted on `newvalue` (sent an email, charged a card). The reversal did not
un-observe; it added an observation. *Mitigation:* none possible - this is §4.1
over the network, and the design's response is to **state it, not fix it**: the
reversal is enumerated on the boundary surface as its own outbound crossing
(§5/G8), the header says reversal never un-observes, and an operator whose
endpoint has subscribers should classify it `compensate`-grade or bare
`emission`, not witnessed, if the intermediate observation is itself the hazard.
**OPEN by nature**; the mitigation is disclosure and the classification choice.

**Attack 5 - the network inverse fires during a 250 fork rewind.** Covered in
§4 and repeated here as an attack because it is the cross-item CRITICAL from item
250's side. A fork rewind that treated a network inverse like an fs inverse would
issue a speculative remote `PUT` per explored branch. *Mitigation (closed):* the
network cap scope (§4) makes the 250 rewind predicate enumerate-not-run it; the
invariant `witnessed[net.*] ⟹ not run by fork rewind` is pinned in tests on both
sides. This one is **closed**, conditional on the scope invariant never being
relaxed - which is why §4 forbids a host-confined-looking network inverse.

---

## 7. Sliced implementation plan

> **Rewritten by the 2026-09-01 revision.** The original Slice 1 (a
> `witnessed[net.*]` proof-surface write) cannot compile against landed code -
> see the Revision section's CRITICAL. The slice below is the compensate-grade
> replacement and is the one that ships.

**Slice 1 - the smallest landable core (py tier,
`PUT`-with-`GET`-preimage COMPENSATE-grade reversal).** The minimum that proves
the move end-to-end on one tier, with **no proof-surface / `noResidue` claim**:

1. `import openapi`: the `x-revl-compensate` / `x-revl-preimage` / `x-revl-undo`
   annotations and their `--compensate`/`--preimage`/`--undo` engineer
   equivalents, honored **only on `PUT`** (verb gate, attack 3), attaching the
   item-247 `compensate` slot to the operation's `emission[net.<host>]` extern
   with a network cap scope; **hard refusal on `POST`/`PATCH`**; the
   claim-vs-proof header block (§2/§3 as revised), stating compensate-grade /
   audit-surface, the reversal-never-un-observes sentence verbatim, and either
   `If-Match` required or best-effort-may-clobber (HIGH 1).
2. The generated `emission[net.<host>] fn ... compensate http_restore(...)`
   shape, reusing item 247's landed `compensate` slot (`parser.py:1022-1028`,
   `lower.py:1577-1602`, `backends/python/emit.py:939-942`). The reversal fires
   Phase-2 on abort, after inverse replay, on the audit surface. The captured
   preimage (URL + bytes + reversal method/headers) rides the compensation
   closure, not a proof-grade witness. **No `HttpWitness`, no `noResidue`.**
3. The **network cap scope** on the IR node, and the corrected item-250
   predicate (HIGH 2) pinned as tests - the pure-net case **and** the mixed
   `compensate[fs, net.x]` taint case (`exists cap in scope: cap not
   host-confined ⟹ enumerated-not-run`) - so a net-tainted compensate-reversal is
   never speculatively fired on a fork rewind, even though 250 itself is not in
   this slice.
4. py-tier host-body stub + one fixture endpoint round-tripped through
   `compile_files` (as `test_import_openapi.py` already does), plus an
   audit-enumeration test that the reversal shows on the G8 / `erase_report`
   surface as its own outbound crossing, tagged compensated-not-undone.

Deferred, explicitly:

- **`DELETE`-recreate compensate-grade** (attack 2's soft-delete residue) - Slice
  2; the same audit-surface framing, recreate via the documented create op.
- **`POST`-with-documented-delete compensate-grade** - Slice 2 follow-up on item
  247's surface.
- **A genuine proof-surface network witness** - a NEW language construct
  (`witnessed-with-observable-inverse`), its own item, not this feature; see the
  Revision section. Slice 1 does not deliver it.
- **Other tiers** (ts/rust/…) - Slice 3, following item 243 Slice 2b's per-tier
  runtime-seam contract (rust waits on item 278 as 243 does).
- **Endpoints with no version token** - Slice 1 either refuses the
  compensate-reversal or marks it best-effort-may-clobber (HIGH 1); a richer
  concurrency-token story is Slice 2.
- **Verified-idempotent upgrade** (attack 3 closure, item 37) - its own item; Slice
  1 ships the verb gate + honesty, not the round-trip proof.
- **item 248 measurement extension** to the network boundary - rides item 248,
  reads the emission/compensation fractions this slice produces.

Slice 1 is additive: no existing generated program uses `x-revl-compensate`, so
every current `import openapi` fixture emits byte-identically, and the backends
are untouched beyond the py host-body stub.
