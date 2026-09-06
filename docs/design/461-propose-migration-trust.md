# 461 (provisional): what a proposal may inherit. Live-state migration across the `propose` trust boundary

**Provisional roadmap id.** This note is filed against GitHub issue
[#100](https://github.com/inso1337/revl/issues/100) (roadmap item 334, the
self-extending runtime). The number 461 is a placeholder chosen so the file
sorts after the last numbered design note; the orchestrator assigns the real
item number at merge and renames this file. "This item" below means issue #100,
and the change it specifies is a hardening slice of item 334, not a new item.

Design only: no compiler change, no `src/` change, nothing implemented here.
Companion docs:
[334-self-extending-runtime.md](334-self-extending-runtime.md) (slices 1 to 3),
[../state-handoff.md](../state-handoff.md) (item 53, the `handoff` declaration),
[../design-v2-instances.md](../design-v2-instances.md) (item 10, instance
migration and its reorder hazard),
[../service-compat.md](../service-compat.md) (the section-5 relation),
[443-estop.md](443-estop.md) (the verdict-shape argument this note reuses).

Source of record for the claims about today's code (line numbers as of
`ee35cea`): `src/revl/gate.py` (`propose` at :623, the standalone decision
compile at :738, the transition compile `_compile_candidate_composition` at
:811, the swap call at :757, the migration report at :799),
`src/revl/mcp/session.py` (`swap` at :896, instance capture at :939, hand-off
capture at :947, `_reconcile_instances` at :1077, `_abort_swap` at :1109,
`_capture_provider_state` at :1263, `_restore_provider_state` at :1291,
`rollback` at :1339), `backends/python/runtime.py` (`SpawnHandle.capture_state`
at :896, `check_state` at :915, `MapHandle.__revl_state__` at :5200),
`src/revl/compiler.py` (`_running_handoffs` at :1165, threaded into the
ambient at :789 only when a `manifest=` is passed), `src/revl/admission.py`
(`_handoff_compatible` at :376, `_admit_handoff_replacement` at :384),
`src/revl/mcp/server.py` (`_tool_swap` at :832, its `SESSION.swap` at :893).

Every finding below was reproduced on `ee35cea` with a throwaway script over
the real cordis-py runtime (the reproducer shapes are restated as exit tests in
section 8). Nothing here is a host-code escape and nothing leaves the
revertible envelope slices 1 to 3 established; this is a disclosure and
soundness hardening of a boundary that was drawn for services and not yet for
state.

## 1. The question

Slice 3 made `Gate.propose` inherit `Session.swap`'s default
`migrate="generational"`: a running composition's live instance state (item
10) and provider hand-off state (item 53) are captured before teardown and
re-seated onto the agent-authored successor. That is the right feature; a
self-extension loop that cold-starts every stateful component on every
proposal is a restart with extra steps.

It also moves state across the one boundary item 334 spent three slices
drawing. Slice 1 bounded what a candidate may REACH (`untrusted_author`: no new
extern, no transitive host-extern reach, no ungranted service). Slice 2 bounded
what it may CLAIM (no realm, no decider, a halt dominates). Neither said
anything about what a candidate may INHERIT, because until slice 3 a proposed
swap inherited nothing. The question this note answers is therefore narrow:

> When generation N holds live state and generation N+1 is agent-authored,
> under what contract does that state cross, and who wrote the contract?

Today the answer is "by name, checked by Python class, contract written by the
candidate". Section 2 shows why that is the answer and what follows from it.

## 2. The two capture paths, and where the contract lives

`Session.swap` reconciles two live layers, both keyed by something the
successor's author writes.

**The instance path (item 10).** `_capture_instances` (:1056) walks gen N's
`manifest.templates`, snapshots each live instance's resource vector through
`SpawnHandle.capture_state` (an ordered list of `(python class, state)` pairs,
`runtime.py:896`), and records whether the template NAME survives into the
successor's `manifest.templates`. `_reconcile_instances` (:1077) then requires,
per template name, the same instance count and a resource vector of the same
length with the same Python class at every position (`check_state`,
`runtime.py:915`), and writes the snapshots in through `__revl_restore__`.
Both correlation keys are candidate-written: `manifest.templates` is derived
from the candidate's own `spawn` targets, and the resource vector is whatever
`let ... = effect Map.new()` lines the candidate chose to write. There is no
declaration on gen N's side at all; a template with live state exports it to
any successor that spells the same name and acquires a same-shaped vector.

**The provider path (item 53).** `_capture_provider_state` (:1263) walks gen
N's components for a `handoff` declaration and snapshots that component's
activation-frame resource vector, keyed by the provided key.
`_restore_provider_state` (:1291) re-seats it onto the successor component
that declares a `handoff` on the same key, after the same length-and-class
check. Here gen N DID write a contract: `handoff cache: Map[Str, Str]` is,
per `docs/state-handoff.md`, "when this component is replaced, that type is
the value it exports". Item 53 checks that contract at admission, with the
section-5 relation `compatible(accepted, exported)` in
`_admit_handoff_replacement` (`admission.py:384`), against the running
composition's exports in `ambient["handoffs"]`.

The load-bearing detail is how those exports reach the ambient:
`compiler.py:789` builds `ambient["handoffs"] = _running_handoffs(manifest)`
only when the compile is given a `manifest=`. `Gate.propose` compiles the
candidate STANDALONE on purpose (slice 1's HIGH finding: a manifest compile
would expose the base's ambient services subject only to `granted`), in both
the decision compile (:738) and the transition compile (:811). So the one
typed contract the migration surface has is never consulted at the one door
where the successor's author is untrusted. The MCP `revl_swap` verb does pass
`manifest=SESSION.ir` (`server.py:872`), so it keeps the item-53 gate, but it
too swaps under the default generational policy (:893), so the instance-path
findings below apply to it as well when its inline source is agent-authored
(slice 2's amendment made that door untrusted-author by default).

## 3. Findings, each reproduced on `ee35cea`

**F1. The item-53 type gate does not run for a proposed swap.** Operator gen N
declares `handoff cache: Map[Str, Str]` and holds `k -> "v1"`. A candidate
declaring `handoff cache: Map[Str, Int]` with `fn get(k: Str) -> Opt[Int]` is
refused by a manifest compile (`state hand-off on 'cache' differs from the
running manifest ... accepts Map[Str, Int], but ... exports Map[Str, Str]`) and
ADMITTED AND SWAPPED by `Gate.propose(..., granted=[])`, which reports
`migration.handoff.cache = {migrated: True, resources: 1}`. The successor's
`get("k")`, typed `Opt[Int]`, returns the string `"v1"`. The runtime
defence-in-depth (`type(res) is old_type`) compares `MapHandle` to
`MapHandle` and cannot see element types. Slice 3's own claim, "a successor
that cannot hold gen N's live state is rejected by the state-compat gate",
holds for the vector's shape and not for its type at this door.

**F2. The instance path has no type contract at any door.** The same retyping
through a template (`Worker` typed `Map[Str, Int]` in the successor,
`Map[Str, Str]` in gen N, no `handoff` anywhere) is admitted and migrated
through `propose`, and the successor's `read`, typed `Opt[Int]`, returns
`"42"`. This one is not specific to the trust boundary (a trusted
`Session.swap` behaves the same, it is the family `test_instance_migration.py::
test_reorder_hazard_...` already pins), but at the boundary the party that
writes the correlating name and the receiving type is the untrusted one.

**F3. Migration is a value channel outside `granted`.** Gen N's `Supervisor`
requires an operator service `Vault` and seeds its spawned `Worker`'s map with
`vault.token()`. A candidate that never names `Vault`, granted nothing, spells
a `Worker` with a one-`Map` vector and adds `fn read(k)`: `propose` admits and
swaps (R2 has nothing to refuse, the candidate reaches no service), and
`read("token")` returns the vault's product. R2 bounds which SERVICES a
candidate may reach; the product of an ungranted service crosses by template
name. Item 334's CANNOT row ("the reach: `granted` only, R2") is therefore true
of capabilities and not yet of data, and the note's item-256 sentence ("the
self-extension loop cannot launder a bound key out through the component it
just admitted") rests on a type-level marking the successor's unchecked types
do not carry. The `confidential` value registry still redacts a `Secret`-marked
value in the host trace after migration (it matches by value, not by type),
but the tool RESULT is not a trace, and after F1/F2 the successor's
declared return type is whatever the candidate wrote.

**F4. Hand-off capture ignores the `migrate` policy, so `rollback` carries
state the wrong way.** `handoff_pre = self._capture_provider_state(old_ir)`
(:947) runs unconditionally, and `_restore_provider_state` (:1017) is gated
only on `handoff_pre` being non-empty, never on `migrate`. `rollback` (:1339)
swaps with `migrate="respawn"` and documents "no cross-generation state
migration back", but a provider that declares a `handoff` does migrate back:
load gen N (`Cache` v1), swap to v2, `put("k2", "from-v2")`, `rollback()`, and
gen N's `get("k2")` answers `"from-v2"`. For this note the direction matters:
an operator who rolls a proposal back gets the AGENT generation's writes
re-seated into operator code through a channel the docstring says is closed.

**F5. A template-level `handoff` is admitted, gated, and then reported
wrong.** The parser accepts `handoff store: Map[Str, Str]` inside a template
(the key is one the template provides), it lowers to
`component["handoff"]`, and the manifest compile's item-53 gate refuses a
retyped acceptor for it. But the runtime capture path looks the template up
as a root fiber (`driver.fibers.get(comp["name"])`, which a template does not
have), captures an empty vector, and reports
`handoff.store = {migrated: True, resources: 0}` beside the instance path's
`templates.Worker = {migrated: True, resources: 1}`, which is where the state
actually moved. The declaration surface this note needs for templates already
exists; it is just not wired to the path that carries template state.

None of F1 to F5 lets the candidate run host code, reach a service it was not
granted, mint a realm, or escape the revertible envelope. What they let it do
is READ and RETYPE state it did not write, with no operator declaration in the
loop, and F4 lets that state flow back into operator code on the one verb an
operator reaches for after a bad proposal.

## 4. Why "safe as-is" does not hold, and why the two obvious fixes are wrong

**The safe-as-is argument, and its limit.** One could say: the operator chose
to let an agent replace the composition, gen N's services were already
callable through `Gate.call`, and a stateless successor was always free to
retire the state, so nothing new is exposed. The limit is F3. A service's
interface is the operator's DECLARED view of its state (`get(k)` needs a key
you know; an aggregate interface exposes no rows); the migrated map is the raw
store, and it can hold what only an ungranted service could produce. Slices 1
and 2 were written on the principle that a candidate's reach is what the
operator granted and nothing else; a channel that carries an ungranted
service's product to a candidate granted nothing is a hole in that principle
even though it is not a hole in the envelope. It should be a declared
crossing, not an incidental one.

**Blanket respawn for `propose` is wrong, twice.** Forcing
`migrate="respawn"` at the door regresses the shipped and tested slice 3 (a
warm proposal is the feature), and it does not even close the surface: F4
shows the hand-off path is not conditioned on `migrate`, so a respawn-forced
`propose` would still carry every declared hand-off across and would only
stop the instance path. A policy that is silent about half of the surface is
not a policy.

**A same-author gate is the wrong key.** "Migrate only when gen N and gen N+1
share an author" needs per-generation trust bookkeeping the session does not
keep (`origin` is source provenance for persistence, not trust; a generation
reached through `rollback` has no clean answer), it refuses exactly the case
slice 3 shipped (operator base, first proposal, warm), and it answers the wrong
question. Whether operator state may cross to an agent successor is a property
of THAT STATE, decided by the operator who owns it, not a property of who
happened to write gen N. The slice 2 amendment already settled the analogous
question for realms: the distinction is the door the source arrives through,
not the function that is called.

## 5. Decision: a disclosure gate, spelled `migrate="declared"`

State crosses an untrusted-author door only through a contract the exporter
wrote and the acceptor wrote, checked by the compiler at the door. The
contract already exists (item 53's `handoff`); this note connects it to both
paths, runs it at the door that skipped it, and turns "undeclared" from a
silent default into a refusal that names its repair.

**D1. A third reconciliation policy.** `Session.swap` gains
`migrate="declared"`, beside `"generational"` and `"respawn"`. It is
generational restricted to DECLARED state: a resource vector crosses onto the
successor only where gen N's component declares a `handoff` on a key it
provides (the export) and the successor's re-provider of that key declares a
`handoff` too (the accept). Everything the generational policy does for a
declared pair (capture before teardown, whole-cohort check then apply,
`_abort_swap` on a shape mismatch, the honest resource count in the report) is
unchanged for it.

**D2. Both paths, one declaration.** The instance path consults the
template's own `handoff` (F5 shows it already lowers and is already gated at a
manifest compile). Under `"declared"`, `_capture_instances` records the
template's export declaration beside its snapshots, and `_reconcile_instances`
requires the successor template to declare an accept on the same key before it
correlates instances. Correlation itself stays as it is (template name,
instance order, positional resource vector): the reorder hazard is
trust-independent and belongs to item 10's stable-key follow-up, which this
declaration is the named prerequisite for (`design-v2-instances.md`,
"reorder hazard"). The provider path is unchanged in mechanism; the template
report in F5 is corrected so a template's `handoff` is reported once, on the
instance path, with the resources that actually moved.

**D3. The type check runs at the door.** `Gate.propose` keeps its standalone
compile (slice 1's reason has not changed) and, after the decision compile,
runs `_admit_handoff_replacement` over the candidate document's components
against `_running_handoffs(self._session.ir)`. That ambient is the hand-off
SHAPES of the running composition and nothing else: no service, no key, no
provider of the base becomes reachable through it, so the standalone
guarantee holds. A mismatch is the item-53 refusal, verbatim
(`state hand-off on 'k' differs from the running manifest`), returned as data
with `admitted=False` and the same `G2` classification a manifest compile
gives it. F1 closes here. `revl_swap` already runs this check through its
manifest compile and needs nothing.

**D4. Live undeclared state refuses BEFORE teardown, with a verdict the loop
cannot mistake for a retry.** When gen N holds a live instance or provider
whose resource vector is non-empty and its component declares no `handoff`,
and the successor would take that template or key over, a `"declared"` swap
refuses before `_dispose_all`, with gen N byte-identical (nothing was torn
down, nothing reloaded, no `_abort_swap`). `propose` reports it as
`admitted=False, code="STATE_UNDISCLOSED"`, naming the template or key, the
resource count, and the one line to add (`handoff <key>: <type>` in gen N's
component). The verdict is `admitted=False` for the same reason `HALTED` is
(slice 2): the candidate was not judged wanting, the composition was, and the
missing line is the OPERATOR's to write. `SWAP_REVERTED` is the retry-shaped
verdict a loop answers by generating another candidate; no candidate can
supply a declaration on gen N's side, so returning it here would have the loop
propose at the wall forever, exactly the mislabel slice 2 removed for the
E-Stop. Dropping the state instead (a cold start) was considered and rejected
on item 53's own rule: silently dropping live state on a swap is residue.

**D5. A declared export the successor declines stays an explicit, reported
choice.** Item 53's rule that a successor without a `handoff` "opts out of
inheriting the state (a deliberate, if lossy, author choice)" is kept: the
agent may retire the state, as it may retire the key. What changes is that
the opt-out is REPORTED. `_restore_provider_state`'s silent `continue` (:1312)
becomes a report entry `{key: {"migrated": False, "reason":
"successor declares no hand-off", "resources": N}}` and the instance path does
the same per template, so `ProposeResult.migration` names every live vector
that did not cross. Nothing about the live system is decided by this; it is
the honesty rule from item 53's report clause applied to the drop case.

**D6. Direction: restore follows the policy, capture does not.** Hand-off
capture stays unconditional, because `_abort_swap` needs the pre-teardown
snapshot to re-seat gen N's own provider state after a REJECTED swap (that is
the item-53 rollback-on-reject, and it must work under every policy). The
RESTORE onto the successor is what follows `migrate`: under `"respawn"`
nothing is re-seated onto the successor, on either path. `rollback` then does
what its docstring says, and F4 closes. A warm rollback is a legitimate
operator wish (keep the writes made under the rejected generation), and it
becomes an explicit `rollback(migrate="generational")` opt-in rather than the
undocumented default; that opt-in is a follow-on, not part of this slice.

**D7. Which doors.** `Gate.propose` always swaps with `migrate="declared"`;
every proposal is agent-authored, so every proposed swap is an untrusted
accept, whoever wrote gen N. The MCP `revl_swap` verb uses `"declared"` when
its inline source compiled under the untrusted-author profile
(`AuthoringTrust` closed, the default) and keeps `"generational"` under
`--author-trust trusted`. The operator doors (`revl swap` CLI, jailed `files`,
`Session.swap` called directly by host code) keep `"generational"` untouched:
the operator wrote the successor, and their declaration (or its absence) is
their own decision, exactly as it is today. No per-generation trust ledger is
introduced; the door decides.

**What this deliberately does not do.** It does not touch item 10's positional
correlation (the reorder hazard stays pinned by its characterization test); it
does not refuse a candidate that retires a stateful key; it does not change
`Session.admit` (a per-turn source is additive and migrates nothing); it does
not sandbox what a successor does with state it was legitimately handed.

## 6. What a proposal can and cannot change, after this slice

The table in `334-self-extending-runtime.md` gains one row and one cell.

| | |
| --- | --- |
| CAN change | which component implements a key; the revl-level composition over the granted providers; the set of keys served (subject to the health gate); which DECLARED state it accepts, at a type section-5-compatible with the export |
| CANNOT change | the host code (G8); the reach, of services AND of state (R2 for services, the disclosure gate for state); the decider; its own authority address (G9); whether a halt applies (`HALTED`); whether undeclared live state crosses (`STATE_UNDISCLOSED`, the operator's line to write) |
| MUST pass | the standalone decision compile under `self_extension(granted)`; the item-53 hand-off type check against gen N's exports, at the door; the activation gate and the post-activation health gate; the `"declared"` reconciliation, with `_abort_swap` to gen N on any shape mismatch |

The sentence in 334's G8 section about item 256 should be narrowed to what is
now true: a `Secret`-marked value keeps its trace redaction across a
migration, and its declared type crosses only through an export the operator
wrote and an accept the compiler checked against it.

## 7. Adversarial self-review of the policy

**A1. The candidate spells the template name and declares an accept.** Needs
an export on gen N's side; without it the swap refuses before teardown (D4).
With it, the operator said this state may leave at this type, which is the
whole decision. Closed.

**A2. The candidate declares a WIDER accept** (`Map[Str, Opt[Str]]` over an
export of `Map[Str, Str]`). Section 5 admits the widening; the successor's
types are then honest about the values it holds, which is all the type gate
is for. Not a hole.

**A3. Transitive inheritance across agent generations.** Gen N+1 (agent)
accepted operator state under a declaration, declares its own export, and gen
N+2 (agent) accepts it. The operator's disclosure was made at N to N+1; what
the agent declares after that is the agent's contract over state it already
holds. That is by design, and it is why the policy is keyed on the door and
the declaration, not on generation lineage.

**A4. Two same-typed resources in one declared template.** The declaration
types the vector as one value (item 53: "at most one hand-off, whose type
describes that whole vector"); a two-`Map` template with one `Map` declared
passes the type gate and the positional runtime check exactly as it does
today, reorder hazard included. This slice narrows the boundary; item 10's
stable-key correlation is the fix for the hazard and stays named, not solved.

**A5. Retiring the template to make the state vanish.** A candidate that does
not spawn `Worker` at all drops its instances with gen N's teardown. That is a
drop the health gate already permits (a key may be retired) and D5 reports it;
it discloses nothing. A candidate cannot both retire the template and read
its state.

**A6. Racing the pre-teardown refusal.** D4 decides from `old_ir`, the live
instance registry, and the candidate document, all read before any teardown
and inside the single synchronous gate (item 332), so there is no window in
which gen N is gone and the refusal has not yet been decided. The check is a
pure function of inputs the swap already holds at :939.

**A7. The `STATE_UNDISCLOSED` message as a channel.** It names a template or
key, a resource count, and a type the operator declared or should declare.
It does not carry state. A candidate that wanted to learn whether gen N holds
state could already tell from `ProposeResult.migration` after an accepted
swap; the refusal reveals nothing an accepted proposal would not.

## 8. Sliced plan and exit tests

**Slice A (py, one PR): the policy at `Gate.propose`.** `migrate="declared"`
in `Session.swap` (D1, D2, D6 restore-follows-policy), the door-side hand-off
type check (D3), the pre-teardown `STATE_UNDISCLOSED` refusal (D4), the
reported opt-out (D5), the F5 report fix. `Gate.propose` switches to
`"declared"`. The slice 3 fixture `_instances_source` in
`tests/test_self_extending_334.py` gains the one export line
(`handoff store: Map[Str, Str]` in `Worker`) and its four tests keep their
assertions unchanged: a declared warm proposal still carries the instance's
state, still bumps `ver`, still reports the same migration block. The
feature slice 3 shipped is intact; what changes is that the fixture now says
so in source.

Exit tests for slice A, each a direct restatement of a section-3 reproducer:

1. *F1 closed.* Operator `Cache` exports `Map[Str, Str]`; a candidate
   accepting `Map[Str, Int]` gets `admitted=False`, code `G2`, message
   containing `state hand-off on 'cache' differs from the running manifest`,
   and gen N still answers `version() == 1` with `get("k") == "v1"`.
2. *F2 closed at the door.* The same retyping through a declared template
   (`Worker` exports `Map[Str, Str]`, candidate accepts `Map[Str, Int]`)
   refuses identically; a candidate accepting `Map[Str, Str]` migrates and
   `read("alice") == "42"`.
3. *F3 closed.* Gen N's `Worker` seeded from an ungranted `Vault`, no
   `handoff` declared; a `granted=[]` candidate spelling `Worker` gets
   `admitted=False`, code `STATE_UNDISCLOSED`, a message naming `Worker`,
   `1 resource`, and `handoff store:`; `driver.fibers` and the live-instance
   registry are the same objects before and after (nothing torn down); gen N
   still answers `read("token")`. Then the operator adds the export line, the
   same candidate (now declaring the accept) is admitted, and
   `read("token")` answers through the successor: the crossing happened, and
   it happened under a declaration.
4. *D5 reported.* Gen N exports `cache`; a candidate that re-provides `cache`
   with no `handoff` is admitted, swapped, and
   `migration.handoff.cache == {"migrated": False, "reason": ..., "resources": 1}`;
   `get("k")` on the successor is `None` (cold, as item 53 specifies).
5. *F4 closed.* `Cache` v1, swap to v2, `put("k2", "from-v2")`, `rollback()`:
   gen N answers `version() == 1` and `get("k2") is None`; the rollback state
   carries no `handoff` block. A trusted `Session.swap(..., migrate="generational")`
   over the same pair still carries `k2` (the generational policy is
   untouched).
6. *F5 fixed.* A template with a `handoff` reports its migration once, under
   `migration.templates.Worker` with `resources: 1`, and no `handoff.store`
   entry with `resources: 0`.
7. *Inertness.* `tests/test_instance_migration.py`,
   `tests/test_state_handoff.py`, `tests/test_state_handoff_exec.py` and the
   stateless cases of `tests/test_self_extending_334.py` pass unchanged: a
   trusted swap, a stateless swap, and a declared provider hand-off are
   byte-identical to before.
8. *Pre-teardown.* Exit test 3's refusal leaves `Session._generation`
   unchanged and writes no swap record to the history (`_record_generation`
   never ran), which is the machine-checkable form of "nothing was attempted".

**Slice B (py, small): the `revl_swap` door.** `_tool_swap` passes
`migrate="declared"` when the candidate compiled under the untrusted-author
profile and `"generational"` under `--author-trust trusted`. Exit test: the
F3 shape through the MCP verb with inline source refuses `STATE_UNDISCLOSED`
by default and migrates under `--author-trust trusted`;
`tests/test_mcp_authority_gate.py` keeps its guard that no sibling verb
reaches `compile_files` unprofiled.

**Slice C (follow-on, named not solved):** `rollback(migrate=...)` as an
explicit warm-rollback opt-in; item 10's stable-key correlation, for which the
template `handoff` this slice wires in is the declared surface
`design-v2-instances.md` asked for; and the rust host, still blocked on the
`revl-gate` crate's missing session layer, unchanged from 334's own status.

## 9. Self-host check

`selfhost/` carries the lexer's `handoff` keyword and lowering already (item
53 landed it there); this slice adds no syntax and no IR field. The door-side
check, the reconciliation policy and the report are session and runtime
code, outside the compiler oracles. No self-host port is needed; item 429's
"does this need a port" answer is NO, for the same reason item 53's runtime
threading needed none.
