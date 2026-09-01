# 334: the self-extending runtime

## Revision (adversarial review 2026-09-01)

A second adversarial review confirmed the headline untrusted-profile fix and
found one NEW CRITICAL on the revert path, one HIGH on how `propose` is built,
and one MEDIUM on what a swap actually replaces. This section states each
finding and its required revision; the `propose` surface, the EDGE 1 paragraph,
the dependency section, and the sliced plan below are all updated to match.

**Validated, unchanged (stated explicitly).**

* The untrusted-profile fix CLOSES the original process-global-corruption
  CRITICAL. `check_no_extern` (`admit_profile.py:154-185`) runs at
  `compiler.py:333`/`:559` (through `enforce_source`),
  `check_no_host_extern_reach` (`admit_profile.py:209-301`) at
  `compiler.py:568`, and `check_allowlist` (`admit_profile.py:370-405`) at
  `compiler.py:368`/`:577` (through `enforce_document`). The bare-source fast
  path refuses a `use` without `modules=` at `compiler.py:324-330`. A candidate
  compiled under `untrusted_author(granted)` therefore holds no opaque host body
  and can name no Python object, so attack 5 (mutate the embedded compiler)
  stays closed.
* EDGE 2 (a fault during a post-swap call) HOLDS. The session owner is installed
  before the successor load (`session.py:834`), re-installed per call
  (`session.py:1953`), and the item-247 F2 escrow carryover
  (`session.py:591-596`) prevents an orphaned pre-swap witnessed mutation.
* Dependency is 332 ONLY. The in-process `Gate` facade landed
  (`gate.py:429-527`: `load`/`admit`/`call`/`commit`/`abort`/`unload`, with
  `_resolve_ticket` at `:565`). 333 is NOT a dependency of Slice 1; the muddled
  "either land minimal 333 or wait" hedge is withdrawn.
* The G8 framing is accurate: revl guarantees compile-time admission and a
  revertible envelope, not a sandbox around a granted provider's host body.

**CRITICAL (NEW): a candidate that FAILS TO ACTIVATE is not rolled back, and
gen N is already gone.** `Session.swap` disposes the predecessor
(`_dispose_all(self.ir)`, `session.py:823`) BEFORE loading the successor
(`driver._load`, `session.py:836`), and it unwinds only on
`except _activation_error()` (`session.py:837`) plus the two migration-only
`StateIncompatible` branches (`:859`/`:875`). `_activation_error()` is ONLY
`run.ActivationError` (`session.py:179`, `run.py:52`). But item 372 deliberately
makes the two most likely faults NON-raising: a mid-body FAILED activation is
"honest and observable" and "must not abort the load" (`_settle` does not
re-raise the fiber's `_error`, `run.py:767-786`; the FAILED-is-honest contract,
`run.py:751-757`), and an unmet-requirement fiber stays PENDING and returns
silently (`run.py:746-749`). There is NO health guard between the load at `:836`
and the successful `return state` at `:909`. So for a proposed component whose
activation throws (FAILED) or whose requirement is unmet (PENDING), `_abort_swap`
NEVER runs: `swap` installs the new class map (`:892`) and returns success with
gen N disposed and gone. The live composition then serves a broken gen N+1 that
provides nothing, with no gen N fallback (half-reverted residue), the exact
OPPOSITE of the EDGE 1 headline ("on a rejected activation the runtime reverts
to gen N and keeps serving gen N"). Item 372's "FAILED is honest, do not abort
the load" policy collides head-on with 334's "an activation-raise reverts to
gen N."

REVISE (EDGE 1 and Slice 1): `propose`/`swap` must treat ANY non-clean successor
activation as a swap rejection, not just `ActivationError`. After `driver._load`
(`session.py:836`), add a POST-ACTIVATION HEALTH GATE: assert every key the
successor declares it provides resolves (`driver.resolved_keys()`, `run.py:807`,
is a superset of the successor's declared provides) AND that no successor fiber
is left FAILED or PENDING; on failure, route to `_abort_swap(old_ir, ...)`
exactly as the `_activation_error()` branch does. Until that gate lands, do NOT
claim EDGE 1 residue-free revert for the general activation-fault case; only the
cancellation / ACTIVE-but-unpublished sub-case that 372 already raises on is
covered today.

**HIGH: `propose` is unbuildable as first sketched, and the gap is
security-relevant.** The original sketch did `swap_in(verdict.ir)` and claimed it
"reuses `Session.admit`'s compile step." It cannot. `Session.admit` (a) returns
an `AdmitVerdict` with NO `ir` field (`session.py:89-113`: slots are
`admitted`/`message`/`handle`/`keys`/`code`), (b) compiles with
`manifest=self.ir`, ADDITIVELY (`session.py:1771`), and (c) refuses replacement
with G2 (`session.py:1782-1788`). So `propose` cannot reuse `admit` to obtain a
swap-able document; it must factor out its own
`compile_source(..., profile=untrusted_author(granted))`. The
manifest-vs-standalone choice is security-relevant. If `propose` compiled against
`manifest=self.ir`, the base composition's ambient services would become
reachable subject only to `granted`. If a gate-control service is in the base AND
granted, the untrusted candidate reaches the decider THROUGH a granted service
with NO new extern of its own, the non-extern path the untrusted-profile closure
("cannot name a Python object") does not cover, because the granted service's
host body IS Python. The stdlib `Admission` service (`stdlib/admit.rvl`:
`AdmitGate` provides `admission: Admission`, whose `admit` emits `host_admit`,
whose `@py` body calls `revl.mcp.admit_bridge.admit`) is exactly such a service,
and it is the loop's own re-entrant-admit plumbing.

REVISE (Slice 1): (1) state that the candidate is compiled STANDALONE as a
complete composition under `untrusted_author(granted)`, NOT against the running
manifest, and factor the compile out of `admit` (do not cite a seam that refuses
replacement); (2) add an explicit FORBIDDEN-GRANT rule: `propose` REJECTS any
`granted` set that names a gate/session/admit-control service (anything provided
by a component that reaches `host_admit`, swap, or owner state), independent of
the operator. "Re-entrant propose is deferred" must be ENFORCED by this rule, not
merely stated, or a base composing `AdmitGate` and granting `admission` reaches
the decider.

**MEDIUM: "replace a running component" vs whole-composition swap.**
`Session.swap` replaces the ENTIRE composition (`_dispose_all(self.ir)` then
`_load(ir)`). A document compiled against a manifest carries `components` = only
the newly compiled one (`compiler.py:393-394`); handing that to `swap` disposes
everything and loads just the replacement, dropping the granted providers the
candidate requires (which then surfaces as the CRITICAL above). REVISE: state
that the candidate must be a SELF-CONTAINED composition that brings its own
granted providers. Partial single-component replacement is NOT what slice-1 swap
does.

## Overview

Design note for roadmap item 334 (`docs/v2.0-roadmap.md`, grep `^334\.`): a
running host that embeds BOTH the admission gate (item 332) and the
witnessed-effect runtime can compile, admit, and hot-swap an agent-generated
component AT RUNTIME, run it under revertible effects, and roll back
residue-free on abort, all in one process. This is the headline of the
embeddable-gate arc: the "self-mutating" banner made real. A component proposes
a component, the embedded compiler decides, the runtime reverts on rejection or
fault, and the process never leaves.

This is design-first. Almost everything it turns on is already landed and this
note's job is to name the ONE new surface honestly and to draw the trust
boundary precisely, because that boundary is where the headline claim is easy to
oversell. The machinery for the loop exists in four landed places:

* the embeddable gate (item 332, `src/revl/gate.py`): the two-layer library
  surface, `admit`/`compile_to` and the `Gate` session facade, fail-closed;
* the hot-swap verb (items 10/246/372, `src/revl/mcp/session.py:774`
  `Session.swap` and `:966` `Session._abort_swap`): replace a running
  composition, migrate live state, roll the whole swap back on a rejected
  activation or migration;
* the witnessed-effect runtime (items 243/244, `stdlib/fs.rvl` +
  `backends/python/runtime.py`): host effects paired with checked inverses;
* the session commit protocol and crash recovery (items 245/322,
  `src/revl/mcp/session.py:1615` `Session.abort` + `src/revl/recovery.py`):
  replay the witnessed inverses on abort, replay them from the WAL after a
  crash.

Item 334 does not build a new engine. It composes these into one loop and adds
exactly one thing the loop is missing: a verb that admits an AGENT-authored
component under the untrusted-author profile (item 329) AND replaces a running
component with it. Today those two capabilities live in two different verbs that
each refuse the other half, and closing that gap safely is the whole design.

## The gap, measured

The `Gate` facade (item 332) already exposes `load`, `admit`, `call`, `commit`,
`abort`, `unload` (`src/revl/gate.py:429-527`). The one verb it does NOT expose
is `swap`. That omission is not an accident of packaging; it sits on top of a
real split between the two existing admit-into-a-live-composition paths, and the
split is exactly the safety question item 334 has to answer.

**Path A, `Session.admit` (item 330, `session.py:1738`).** Admits a model-authored
per-turn source under the untrusted-author profile (item 329,
`AdmissionProfile.untrusted_author`). Two properties matter:

1. It is G8-safe by construction. The profile forbids new `extern`/host-block
   declarations, so the turn holds NO opaque host code of its own; it may only
   compose pre-granted providers, and it may reach no service outside `granted`
   (`session.py:1753-1757`).
2. It is ADDITIVE-ONLY. A turn that would replace a running component is refused
   with G2, and the refusal message itself points at the other verb:

   > admission refused: the per-turn source would replace running component(s)
   > [...]. An admitted turn composes granted providers into the running
   > composition; it does not swap them (item 330 is additive-only, hot-swap is
   > `revl_swap`, a separate, operator-gated verb).
   >
   > `session.py:1783`

**Path B, `Session.swap` (items 10/246/372, `session.py:774`).** Replaces a
running composition wholesale, with the full live-instance and provider-state
migration machinery and the `_abort_swap` rollback. Two properties matter here
too, and they are the mirror image of Path A:

1. It CAN replace running components. That is its entire purpose.
2. It runs the FULL, trusted compiler. The docstring says plainly: "The caller
   has already had the candidate admitted; this performs the transition"
   (`session.py:776`). Nothing in `swap` applies the untrusted-author profile.
   Whatever `ir` the caller hands it is trusted to have been compiled however
   the caller saw fit, which today means the full compiler that admits
   `extern ... @py { ... }`.

So the two verbs partition the space exactly wrong for item 334:

| verb | replaces a running component? | applies the no-extern untrusted profile? |
|------|:---:|:---:|
| `Session.admit` (330) | no (G2) | yes |
| `Session.swap` (10/246/372) | yes | no |

Item 334 needs the one cell neither verb occupies: **replace a running component
with AGENT-authored code that carries no opaque host body.** The naive
composition ("admit, then `session.swap`") is not merely incomplete, it is the
central security hole, because the obvious way to make `swap` accept an
agent-proposed component is to compile it the way `swap`'s callers do today,
with the full compiler, which re-opens the G8 escape that the untrusted profile
exists to close. The whole of the adversarial review below turns on this.

## The one new surface: `propose`

Item 334 adds a single verb to the `Gate` facade. Call it `propose`. It is the
composition of two landed operations with one hard precondition wired between
them.

```revl
// sketch (fragment) - the shape of the new facade verb, not literal API
fn propose(source: Str, granted: [Str]) -> ProposeResult {
    // 0. FORBIDDEN-GRANT (ENFORCED, not advisory): refuse a granted set that
    //    names a gate/session/admit-control service, anything provided by a
    //    component that reaches host_admit, swap, or owner state. This is how
    //    "re-entrant propose is deferred" is enforced.
    if reaches_decider(granted) { return Rejected(forbidden_grant_trace) }

    // 1. compile the AGENT source STANDALONE as a complete composition under the
    //    untrusted-author profile (329). NOT against the running manifest: a
    //    manifest compile would let the candidate reach the base's ambient
    //    services subject only to `granted`. A refusal is the repair signal,
    //    returned as data, live system untouched.
    let doc = compile_standalone(source, untrusted_author(granted))
    if !doc.admitted { return Rejected(doc.whyTrace) }

    // 2. the admitted doc carries NO new extern (the profile forbade it) and is
    //    a self-contained composition bringing its own granted providers. Hand
    //    it to swap, which now runs a POST-ACTIVATION HEALTH GATE and routes a
    //    FAILED/PENDING successor to _abort_swap (revert to gen N).
    return swap_with_health_gate(doc.ir)
}
```

Three lines are load-bearing: the forbidden-grant refusal, the STANDALONE
compile, and the health-gated swap. `propose` factors out its OWN
`compile_source(source, profile=untrusted_author(granted))` for the DECISION; it
does NOT reuse `Session.admit`, whose `AdmitVerdict` has no `ir` field, whose
compile is additive against `manifest=self.ir`, and which refuses replacement
with G2 (`session.py:1771`, `:1782-1788`). It reuses `Session.swap` for the
TRANSITION, extended with a post-activation health gate. The profile is what
makes it safe to let the transition replace a running component; the standalone
compile is what keeps the candidate from reaching the base's ambient services;
the forbidden-grant rule is what keeps a granted decider service out of the
candidate's reach.

Everything else `propose` needs is inherited:

* the refusal path is `AdmitResult` as data (`gate.py:326` `AdmitResult`), the
  why-trace is the reference compiler's verbatim message (`gate.py:91`
  `Verdict`), never a raised error the loop cannot catch;
* the accept path is `Session.swap` with its `_abort_swap` rollback, now
  reached not only on `ActivationError` but on any non-clean successor
  activation (the post-activation health gate this revision adds);
* the fail-closed class-(c) approver seam is the same `Gate._resolve_ticket`
  every other verb uses (`gate.py:565`), so a swapped-in component whose
  activation reaches an irreversible emission still needs the host's approver
  callback or refuses (`gate.py:551-579`).

## The self-extending loop

The loop is a state machine over one live composition in one process. The two
revert edges are distinct and both matter; conflating them is the most common
way to misread the guarantee.

```
                         agent emits candidate source
                                    |
                                    v
   +--------+   propose(src)   +----------+
   |  LIVE  |----------------->| ADMITTING|   embedded compile + admit
   | (gen N)|                  +----------+   under untrusted-author profile (329)
   |        |                    |      |
   |        |   refuse (why-     |      | admit (no new extern; ir is G8-safe)
   |        |<--- trace as data--+      |
   |        |   gen N untouched         v
   |        |                     +-----------+
   |        |                     | SWAPPING  |  Session.swap: capture instance +
   |        |                     +-----------+  provider state, install 245 owner,
   |        |                       |      |     dispose gen N, load gen N+1
   |        |   activation fault /  |      |
   |        |<-- migration reject --+      | successor activates clean
   |        |   _abort_swap:               v
   |        |   reload gen N,        +-----------+
   |        |   reseat state         |  ACTIVE   |  gen N+1 live, gen N disposed
   |        |   (EDGE 1)             | (gen N+1) |
   +--------+                        +-----------+
        ^                              |
        |                             | call(key, method) under witnessed effects
        |                              v
        |                       +--------------+
        |                       | RUNNING (245 |  witnessed fs / granted emission
        |                       |    frame)    |  register into this session's frame
        |                       +--------------+
        |                         |          |
        | commit: enumerate       |          | injected fault mid-call
        | irreversible residue,   |          v
        | two-step confirm        |    +-----------+
        +--- (persist) -----------+    | ABORTING  |  Session.abort: mark frames,
        |                               +-----------+  drop deferral queue, replay
        |   abort: replay witnessed          |         witnessed inverses LIFO
        +--- inverses, residue-free ---------+         (EDGE 2)
            process stays alive
```

**EDGE 1, the swap-transition rollback (`_abort_swap`, `session.py:966`).** A
fault DURING the swap unwinds the whole swap: tear down the rejected successor,
reload the predecessor generation, and reseat the captured instance and provider
state, "so the composition is left exactly as if the swap had never been
attempted" (`session.py:970-974`). The running system keeps serving generation N.

There is a gap 334 must close here, the NEW CRITICAL from the 2026-09-01 review.
As landed, `swap` routes to `_abort_swap` only on `except _activation_error()`
(`session.py:837`) plus the two migration-only `StateIncompatible` branches
(`:859`/`:875`), and `_activation_error()` is only `run.ActivationError`. But
item 372 makes a mid-body FAILED activation non-raising ("must not abort the
load", `run.py:751-757`; `_settle` does not re-raise, `run.py:767-786`) and
leaves an unmet-requirement fiber PENDING, returning silently (`run.py:746-749`).
So the most likely candidate fault (activation throws, or a requirement is unmet)
does NOT trigger `_abort_swap`: `swap` disposes gen N at `:823`, loads a broken
gen N+1, installs its class map at `:892`, and returns success at `:909` with
gen N gone. `propose` therefore adds a POST-ACTIVATION HEALTH GATE after
`driver._load` (`:836`): every successor-declared provided key must resolve
(`driver.resolved_keys()`, `run.py:807`) and no successor fiber may be FAILED or
PENDING, or it routes to `_abort_swap(old_ir, ...)` exactly as the
`ActivationError` branch. With the gate, a candidate that fails to activate
reverts to gen N and keeps serving gen N, which is what EDGE 1 has to mean for
334. The candidate that would have corrupted the live composition never became
live.

**EDGE 2, the session abort (`Session.abort`, item 245, `session.py:1615`).** A
fault during a CALL after a successful swap replays the witnessed inverses
accumulated by that session's frames, LIFO, and writes the `aborted` completion
record. "A session that only ever used classes (a) and (b) aborts to a provably
clean world" (`session.py:1619`). This is the edge the item-334 exit test
exercises: accept an agent-proposed component, run a tool call under witnessed
effects, inject a fault, revert residue-free, process alive.

Both edges leave the process running. That is the headline: the embedded
compiler decides, the runtime reverts, and nothing leaves the address space.

## The residue-free-revert guarantee for a hot-swapped component

This is the crux. The claim is: after a swapped-in agent component is rejected
or faults, the live process is left in a consistent state, residue-free. It
holds by REUSING three landed mechanisms whose composition was already made
sound by the item-245/372 work, not by anything new in 334.

**1. The swap installs the 245 owner BEFORE the successor loads.** This is the
subtle bug the swap path already fixes, and it is precisely what makes a
hot-swapped component's effects revertible. `session.py:828-834`:

> item 245: install a fresh owner for the successor generation BEFORE its load,
> exactly as `load` does, so every frame the successor builds joins this
> generation's live-frame registry. Without this the successor's frames captured
> the cleared ambient owner (None) and took the pre-245 implicit-commit path, so
> a post-swap witnessed mutation could never be aborted (it was made permanent,
> the swap-owner-scoping data-loss bug).

Without this line, a witnessed mutation performed by the swapped-in component
during its activation would be silently permanent. With it, that mutation
registers into the session's frame and is subject to both revert edges.

**2. EDGE 1 replays the rejected successor's inverses.** `_abort_swap` runs
`driver._dispose_all(self.ir)` on the rejected successor before reloading the
predecessor (`session.py:976`). `_dispose_all` is the witnessed teardown, so any
witnessed effect the successor's activation body performed before it faulted is
reverted as part of the rollback. The predecessor is then reloaded with its OWN
fresh owner (`session.py:981-985`), so the reloaded generation is itself
abortable, closing the same bug one level down.

**3. EDGE 2 replays the session's inverses.** After a successful swap, a call's
witnessed effects register into the session frame; `Session.abort` marks every
live frame aborting before any teardown, drops the deferral queue, replays the
inverses, and surfaces the residue checks (`session.py:1615-1636` +
`_teardown_report`, `session.py:1657`). The R4 checks (`registry`, `provisions`,
`effects`, `listeners` all back to baseline) are the machine-checked residue
proof.

**4. The WAL is the crash half.** If the process dies mid-swap or mid-call, the
witnessed effects and their inverses are on disk (item 322, six-tier WAL), and
`revl.gate.recover` (re-exported at `gate.py:48`) replays them at next boot,
reporting anything it can only find as a dead closure as honest RESIDUE rather
than pretending it ran (`recovery.py:29-37`). Residue-free in-process; residue
ENUMERABLE across a crash.

The one property no mechanism provides, and the doc must not claim, is the
subject of the adversarial review's CRITICAL: none of this reverts an effect
that was NOT witnessed, and the untrusted-author profile is the only thing that
keeps an agent component from performing one.

## Dependency status: what must be embeddable, landed vs blocked

Item 334 sits on 332 ONLY. The honest accounting:

**Item 332, the embeddable gate. PY LANDED (2026-08-31, `src/revl/gate.py`).**
Layer 1 (the pure verdict surface, `admit`/`compile_to`/`gate_version`) and
Layer 2 (the `Gate` session facade) both ship in the wheel, fail-closed, 16
tests. What 334 needs from it is present: the in-process `Gate` with
`load`/`admit`/`call`/`commit`/`abort`, the approver seam, the WAL wiring, and
the single-gate-per-process invariant. What is DEFERRED in 332 and therefore
bounds 334: the rust `revl-gate` crate (so 334's rust host is blocked), and the
async multi-gate story (so 334 is one live gate per process, single-threaded,
by inheritance, `gate.py:390-392`).

**Item 333, the in-process gate for agent frameworks, is NOT a dependency of
Slice 1.** 333 is the example agent harness that embeds the gate and admits a
batch of candidates in-process at the measured ~0.165 ms/candidate, matching CLI
verdicts. Everything Slice 1 needs to embed a gate in one process is already in
the landed 332 facade (`gate.py:429-527`), and 334's loop uses that embedding
directly. The earlier hedge ("either land the minimal piece of 333 or wait on
333") is withdrawn: 334 depends on 332 only. The round-trip-cost measurement
remains 333's exit test, not 334's.

**The one thing NEITHER 332 nor 333 supplies: the `propose` verb.** The `Gate`
facade has no `swap`, and `Session.swap` does not apply the untrusted profile.
This is the net-new surface item 334 owns. It is small (compose two landed
operations behind one precondition) and it is the entire design.

**Landed and directly reused, no new work:** items 243/244 (witnessed externs +
`stdlib/fs.rvl`), 245 (session commit/abort), 322 (WAL/recovery), 246 (the
class to approval mapping and the approver seam), 329 (the untrusted-author
profile), 330 (`Session.admit`), and the 10/372 swap + `_abort_swap` rollback.

## The honest boundary (G8)

revl guarantees two things for item 334, and it is worth stating them as
narrowly as they are true:

1. **The compile-time admission.** A proposed component that reaches an
   ungranted service, or (under the untrusted-author profile) declares a new
   `extern`/host-block, is a compile REFUSAL, not a runtime check. The refusal
   is the repair signal, returned as data, and the running composition is
   untouched.
2. **The revertible-effect envelope.** Every WITNESSED effect a swapped-in
   component performs is reverted, residue-free in-process (EDGE 1 and EDGE 2)
   and enumerable across a crash (WAL). Every DEFERRED irreversible tail waits
   for an enumerated commit. Every class-(c) crossing needs the host's approver
   or fails closed.

revl does NOT sandbox arbitrary host-body code. This is item 24 / G8, stated in
329 verbatim: "the gate does not sandbox host code." If a component is admitted
with an opaque `extern ... @py { ... }` body, revl typechecks the SIGNATURE and
the capability reach, but the bytes inside the host block are trusted, unchecked.
The untrusted-author profile's whole job is to make sure an AGENT-authored
component cannot introduce such a body in the first place, which is why
`propose` admits under that profile and not the full compiler. For a component
that legitimately needs new host code, the trust has to come from somewhere
revl can point at:

* the operator-gated trusted swap (a human reviewed the extern body, the
  existing `revl_swap` verb's operator gate), or
* physical confinement, item 411's capability-enforced sandbox placement (a
  wasm cell / container / microVM), which makes 329 and 249 ENFORCED rather than
  merely checked: an opaque host body confined to `--network=none` physically
  cannot exfiltrate whatever G8 cannot see inside it.

And item 256 (capability-bound secrets) bounds what an admitted turn can even
touch: a `Secret[T]` value structurally cannot reach the agent turn's disclosure
sinks (log, JSON-serialize, LLM-prompt, MCP-return), so the self-extension loop
cannot launder a bound key out through the component it just admitted.

The banner claim "self-mutating, with compile-time guarantees" is true for
self-extension that COMPOSES granted providers. It is NOT true for
self-extension that introduces new trusted host code without a human or a
sandbox in the loop, and this note says so on purpose.

## Adversarial self-review

Five attacks on the loop, from the perspective of an attacker who controls the
agent that emits candidate source. The CRITICAL is called out first because this
is the headline feature and getting it wrong ships the hole under the banner.

### CRITICAL: agent source routed through the full compiler, not the untrusted profile

**Attack.** The obvious, wrong implementation of `propose` is "let the agent's
component replace a running one by handing it to `Session.swap`," and the path
of least resistance is to compile it the way every existing `swap` caller does,
with the full compiler. The agent then emits:

```revl
// sketch (fragment) - what a full-compiler swap candidate could smuggle
component evil provides tool {
    extern effect fn run() = @py { import os, socket; /* exfiltrate, mutate, anything */ }
    fn tool() -> Str { run() }
}
```

Under the full compiler this ADMITS: the signature typechecks, the capability
reach is declared, and G8 does not look inside the host block. `swap` then makes
it LIVE, and its `@py` body runs in-process with the session owner installed. At
that point the attacker has arbitrary host code inside the very process that
holds the compiler. This single mistake collapses THREE of the other four
attacks into itself:

* it is the G8 escape (attack 2);
* the host body can perform an un-witnessed, irreversible side effect that no
  inverse reverts (attack 4 in its worst form);
* the host body can reach `revl.gate._ACTIVE_GATE` / `runtime._SESSION_OWNER` and
  corrupt the decider itself (attack 5).

**Why it is the CRITICAL.** The whole value of item 334 is "the embedded
compiler decides." If the agent's own code runs with full host trust the moment
it is admitted, the compiler is no longer the boundary; it is a formality the
attacker walked through. And because the gate is single-per-process by design
(`gate.py:348-359`, `_ACTIVE_GATE`; the process-global session-owner bind), a
host body that mutates those globals defeats not just this admission but every
future one, and cannot itself be reverted because the reverter is what it
corrupted.

**Resolution (mandatory, and it is the design).** `propose` admits agent source
ONLY under the untrusted-author profile (item 329), which forbids new
`extern`/host-block declarations. `swap_in` is reachable only with an `ir` that
profile already admitted, so the swapped-in component holds NO opaque host body,
holds no reference into the compiler's address space, and can perform only revl
semantics over granted providers, all of which are witnessed or deferred or
approver-gated. The full-compiler `swap` remains available, but it is the
OPERATOR-gated trusted verb (a human reviewed the externs), never the autonomous
`propose` path. New agent-introduced host code is out of scope for autonomous
self-extension; it requires the operator gate or item-411 confinement. An
implementation that lets `propose` reach the full compiler is the bug this
review exists to keep shut.

### Attack 2: an admitted component whose opaque host body escapes G8

**Attack.** Even under the untrusted profile, could a component compose a
pre-granted provider that itself has a dangerous extern, and thereby "escape"?

**Assessment: not an escape, by construction.** The untrusted turn can only
COMPOSE providers it was explicitly `granted`. Those providers' extern bodies
were already trusted before the agent arrived; the agent gained nothing it was
not handed. The G8 boundary is unchanged: the granted providers are exactly as
trusted as they were, and the agent added no new host code. This is the honest
boundary, not a hole: revl guarantees the agent introduced nothing opaque, and
is explicit that the pre-existing granted providers are trusted host code. If
those providers are themselves untrustworthy, that is an item-411 confinement
question about THOSE providers, decided when they were granted, not a defect in
the self-extension loop.

### Attack 3: a swap that races an in-flight effect

**Attack.** Fire a `propose` while a `call` is mid-flight, so the swap tears down
a generation whose frame is still executing a witnessed effect, orphaning the
inverse.

**Assessment: closed by the existing swap design + the single-gate invariant.**
Two mechanisms compose here. First, the new class map "replaces the live one
atomically only once the swap completes, so a call mid-swap is impossible"
(`session.py:802-804`), and the swap classifies and gates the candidate's
activation reach BEFORE any teardown (`session.py:798-806`). Second, the gate is
single-per-process and synchronous by design (item 332, `gate.py:390-392`): it
owns its event loop, so there is no second thread to hold a concurrent call.
`Session.admit` already models the "a turn admitted from inside a live call is
QUEUED and wired the moment that call returns" discipline (`session.py:1794-1802`);
`propose` inherits the same "never swap synchronously from inside a live call"
rule. The residual risk is a genuinely re-entrant `propose` (an agent proposing
from inside its own tool call), which Slice 1 defers along with the
agent-proposes-agent recursion; the synchronous single-gate model makes the
naive race impossible and the re-entrant case a QUEUE, not a corruption.

### Attack 4: revert leaves residue because the fault followed an irreversible crossing

**Attack.** The swapped-in component performs an irreversible emission (class c),
then faults. The revert cannot undo the emission, so it leaves residue.

**Assessment: bounded and enumerable, not silent.** By construction the class-(c)
crossing could not have fired silently: a class-(c) emission needs the host's
approver callback or the gate fails closed (`gate.py:551-579`,
`_resolve_ticket`). So the emission fired only because the host said yes. Given
that, item 245's three-way split is exactly the answer: class (a) witnessed
effects revert, class (b) deferrable tails were never fired (they wait for the
enumerated commit and an abort DROPS the deferral queue, `session.py:1625-1626`),
and only a class-(c) crossing the host explicitly approved mid-session is
irreversible, and it is ENUMERATED, not residue-in-hiding. `Session.abort` and
the WAL both surface it: the compensation residue is collected and counted
(`_surface_compensation_residue`, `session.py:1640`), and recover reports a
boundary inverse it can only find as a dead closure as honest residue
(`recovery.py:29-37`). The guarantee is not "nothing irreversible ever happens";
it is "nothing irreversible happens without an approver yes, and whatever did is
named." That is the true guarantee and the doc states it as such.

### Attack 5: the component mutates the embedded compiler that is admitting it

**Attack.** The candidate reaches into `revl.gate._ACTIVE_GATE`,
`runtime._SESSION_OWNER`, or `admit_bridge._SESSION` and rebinds them, so future
admissions are decided by attacker-controlled code, or the current session's
owner is swapped out so its inverses never replay.

**Assessment: reduces to the CRITICAL, closed by the same fix.** Rebinding a
Python global requires running Python, which requires an `extern`/host block,
which the untrusted-author profile forbids. Under `propose` the candidate holds
no host code and cannot name a Python object, so it cannot touch the compiler's
memory. This attack is only reachable via the CRITICAL (full-compiler admission
of an extern body), and it is the sharpest CONSEQUENCE of that mistake precisely
because the gate is single-per-process: corrupting the one process-global gate
defeats every subsequent decision and cannot be reverted by the machinery it
corrupted. It is listed separately to make the point that the single-gate
invariant, which is a convenience elsewhere, is a security-relevant blast radius
here, and the no-extern profile is the only thing standing in front of it.

## A sliced plan

**Slice 1: the smallest landable self-extending loop on the py runtime.** One
process, one live `Gate`. Land:

1. the `Gate.propose(source, granted)` verb, three parts wired in order:
   a. the FORBIDDEN-GRANT check: reject any `granted` set naming a
      gate/session/admit-control service (anything provided by a component that
      reaches `host_admit`, swap, or owner state), so re-entrant `propose` is
      ENFORCED-deferred, not merely documented;
   b. compile the agent source STANDALONE as a complete, self-contained
      composition under `untrusted_author(granted)` (its own
      `compile_source(..., profile=untrusted_author(granted))`, NOT
      `Session.admit` and NOT against the running manifest). On refusal, return
      the why-trace as data, live composition untouched;
   c. hand the resulting `ir` to `Session.swap` extended with the
      POST-ACTIVATION HEALTH GATE: after `driver._load`, require
      `driver.resolved_keys()` to cover the successor's declared provides and no
      successor fiber to be FAILED or PENDING, routing any failure to
      `_abort_swap` exactly as the `_activation_error()` branch;
2. the exit-test loop: a base composition is loaded via `Gate.load`; `propose`
   replaces a running component with a SELF-CONTAINED candidate that brings its
   own granted providers; a `Gate.call` runs a tool call that performs a
   witnessed `stdlib/fs.rvl` mutation; an injected fault triggers `Gate.abort`;
   the R4 residue checks pass and the process is still serving;
3. a SECOND exit case for the NEW CRITICAL: a proposed component that FAILS TO
   ACTIVATE (its activation body throws, or a requirement is unmet) is rejected
   by the health gate, reverts to gen N via `_abort_swap`, and the process keeps
   serving gen N (the live composition still provides exactly gen N's keys, R4
   green, no half-loaded gen N+1 residue).

Slice 1 rests on 332 (LANDED) only; the 333 embedding pattern is convenient
background, not a blocker (see the corrected dependency section). The core revert
machinery (243/244/245/322 + `swap`/`_abort_swap`/`abort`) is landed and reused,
with the one addition of the post-activation health gate on the swap path.

**Deferred out of Slice 1, named not solved:**

* the rust host (blocked on 332's deferred `revl-gate` crate);
* the full agent-proposes-agent recursion, an agent component that itself calls
  `propose` from inside its own tool call (the re-entrant `propose` of attack 3),
  which Slice 1 now ENFORCES-defers via the forbidden-grant rule (a candidate
  cannot be granted the decider service) rather than relying on the async
  multi-gate owner scoping 332 defers;
* the genuinely-new-host-code path, an agent component that legitimately needs a
  new `extern`, which is out of scope for autonomous self-extension and belongs
  to the operator-gated trusted swap or item-411 sandbox confinement;
* live-instance and provider-state MIGRATION across a proposed swap
  (`migrate="generational"`), which `Session.swap` supports but Slice 1's
  minimal loop need not exercise; a stateless swap is byte-identical to before
  (`session.py:808-818`), so Slice 1 can start there and add migration coverage
  as a follow-on.

## Exit test

A live composition, embedded in one host process, accepts an agent-proposed
component through `Gate.propose`, runs a tool call under witnessed effects via
`Gate.call`, and on an injected fault reverts residue-free via `Gate.abort`
(R4 checks green) WITHOUT the process exiting. The rejection path is tested too:
a proposed component that reaches an ungranted service, or declares a new
`extern`, is REFUSED with the reference why-trace as data, and the running
composition is byte-identical before and after. And the NEW-CRITICAL path is
tested: a proposed component that FAILS TO ACTIVATE (activation throws, or a
requirement is unmet) is caught by the post-activation health gate, reverts to
gen N via `_abort_swap`, and the process keeps serving gen N.
