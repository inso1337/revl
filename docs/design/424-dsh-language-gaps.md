# 424: the three language-side gaps from the deepseek-harness comparison

Design note for roadmap item 424. Design only: no compiler change, no `src/`
change, nothing implemented.

Item 424 records a full crawl of DSH's reference against revl and revl-harness.
Most of what it found is harness-side and lives on the harness roadmap. Three
gaps are language-side, because each is a question about what a composition
MEANS that revl has to answer before the harness can build anything on top:

- **(a) row-addressed layered composition.** A third party ships a bundle, a
  user overrides one row, nobody forks.
- **(b) interception with declared reach.** Any DSH plugin can register on
  `tools/pre-execute` and see every tool call; revl has no place for a third
  party to stand.
- **(c) typed remote client generation.** DSH's `typert` generates a host and a
  typed client from a decorated class; revl generates typed boundaries from
  `service` declarations but has no generated client face.

**Gap (a) is not open.** Item 426 had a full design pass land
(`docs/design/426-composition-layers.md`), and §1 below is a citation map from
424(a)'s exit clauses to 426's decisions, plus the five things (a) needs that
426 deliberately does not decide. This note does not re-decide any of it and
does not offer a competing model.

Gaps (b) and (c) are decided here. §4 records the result the three share, which
changes the cost estimate for both.

---

## 0. The three questions, in one table

| gap | the question, in composition-semantics terms | answered where |
|---|---|---|
| (a) | Can a party who did not write a composition change one row of it, with the change checked before it runs? | item 426, in full. §1 is the map. |
| (b) | Can a component that neither the consumer nor the provider named observe or decide calls across a provision edge, with its authority declared, bounded and diffable, and with the edge chosen by the composition rather than by either party's source? | §2 |
| (c) | When a composition consumes an operation that executes in a process it does not own, what object is the remote provider in the composition, and what is checked about it? | §3 |

---

## 1. Gap (a): resolved by item 426

### 1.1 The citation map

424(a)'s exit is three clauses and one claim. All four are decided in 426, and
one of them is decided more sharply than 424(a) stated it.

| 424(a) clause | 426 decision | where |
|---|---|---|
| "give composition rows stable identity" | Decision 1. Identity is a declared label scoped to its origin, `<origin>::@<label>`. Not the claim set, not the component name, not the file, not a hash. | 426 §1 |
| "define patch semantics that target an id" | Decision 2. A patch addresses the provision claim (`key("db")`) or the label (`acme_pg::@db`), never a file and never a component name; an address resolving to nothing is a REFUSAL, never a no-op. The operations are exactly four: `add`, `remove`, `replace` (claim-preserving), `configure`. | 426 §2, §3.2 |
| "re-admit the patched result" | Decision 3 (resolution is a pure fold that never calls the gate, so no intermediate state exists and the resolver is not on the trusted path) and Decision 5 (incremental admission rides `admit_into`; the delta is one `compile_files` call with a `replacing` withdrawal set). | 426 §3.3, §5.1 |
| "the difference is cost, not safety" | Confirmed and quantified: the admission cost is one compile of the patched rows, not of the composition, with the evidence already in `tests/test_manifest.py:44`. | 426 §5.1 |

426 also SHARPENS that last claim in a way 424(a) got wrong. 424(a) says the
difference from DSH is cost, not safety, and treats that as the end of the
matter. 426 §5.2 shows the cost is not uniform: **admission is incremental,
activation is not**, and the blocker is G7 (teardown LIFO plus consumer
re-resolution), not effort. An `add`-only layer rides `_wire_turn` hot; a
`replace` or `remove` costs a whole generation. So DSH's "override one row and
restart" is matched exactly (the restart is in the sentence), and DSH's
"override one row and do not restart" is not, and 426 says so rather than
implying otherwise.

Three further decisions in 426 answer questions 424(a) raised as open:

- **identity of a row under a source edit** (426 §1.4): a provision ADDED
  upstream keeps the label and is reported; a provision REMOVED is a refusal; a
  component RENAMED is a non-event.
- **bundles versus `truc`'s package identity** (Decision 7): distribution is
  truc's, semantics are the composition's, and when they disagree the
  composition is source of truth while the lock is the integrity proof. Item
  136's `[trucs]` table becomes the origin namespace, so there is no second
  identity scheme and no squatting policy to write.
- **what a user approving a bundle is shown** (426 §8): the authority panel,
  with a TRUST BASIS line, crossing tokens re-keyed by row label, a `config:`
  token carrying a value digest, a fail-closed headline, and a printed BLIND
  SPOTS block. This is the part of 426 that did real work, because both of its
  source designs found a different CRITICAL in it and neither fix closes the
  other.

**Verdict: 424(a) is answered. There is nothing here for a second design to
add, and this note adds none.**

### 1.2 What 426 does not decide, and what (a) still needs

Five residuals. Two are already filed by 426 itself, two are small completions,
one is gap (b).

**R1. Activation of `replace` and `remove` (filed, not blocking (a)).** 426
§5.2 ships incremental admission and inherits whole-generation activation, and
§5.3 files the `configure`-only narrowing as the highest-value follow-on.
424(a)'s own user story is "a user overrides one row and restarts", so R1 does
not block (a). It blocks the harness's hot-configuration story, which is a
harness-roadmap item, not this one.

**R2. The `granted` clause has no surface (a completion, not an override).**
426 §9.3 Part 2 decides that the reach allowlist a non-first-party row may
compose against is per-row and that the composition declares it, and says it
"belongs on the row" beside `open` and `reach`. But 426 §6.1's surface shows
only `config`, `open`, `reach` and `component`. An undeclared surface is the
difference between Decision 6 being enforceable and being aspirational, because
`AdmissionProfile.untrusted_author(granted)` takes `granted` as an argument and
something has to produce it. Completing it, in 426's own grammar and without
touching any of its decisions:

```revl sketch
row @otel from "trucs/otel_kit/component.rvl" provides metrics
  granted { clock, metrics_sink }
```

with three rules: `granted` defaults to EMPTY, not to "everything the row
requires"; a row whose `requires` set is not a subset of its `granted` set is
refused at resolution, naming the ungranted key; and `granted` is writable only
in the base composition and the site layer, never in a stack layer, for the
same reason 426 §4.1 gives for trust class (no layer may raise its own
authority). This is a completion of 426 §9.3 Part 2, offered for the architect
to fold in or reject; it is not a new decision about anything 426 settled.

**R3. Placement in a layer (undecided in 426; decided here by citing 337).**
426 §6 point 4 says the composition subsumes `placement.py`'s `[processes]`,
`[tiers]` and `[config.X]`, and shows `place @db on process "provider" backend
rust`. It does not say who may write a `place` clause. The answer follows from
two decisions already taken and does not need a new one:

> **Placement is STRUCTURE, so the invocation overlay may not touch it** (426
> §3.1 rank 3 is values only, never structure), **and a stack layer may not
> write a `place` clause at all.** Only the base composition and the site layer
> may place a row.

The reason a stack layer may not place is item 337's, not an ergonomic one:
337's seam invariant treats each tier boundary as a re-admission point where
the receiver re-compiles from its own source and derives both gate inputs from
independently held state. Moving a row across a process or backend boundary
therefore moves it into a different admission domain. A third party that can
write `place` chooses which gate judges its own row, which is the shape 337's
adversarial review already caught and named admission-theater ("a sender
controlling both gate inputs picks the question"). Letting a stack layer place
reintroduces it one tier up.

**R4. Extension by observation is not extension by composition.** This is the
(a)/(b) seam and it is why item 424 filed two gaps rather than one. Under 426 a
stack layer can `replace` a row, `configure` a row, or `add` a row. An added row
is only reachable if something in the base already `requires` its key; if
nothing does, the added row is inert except for whatever its own activation body
does. So 426's third-party extension story covers substitution, configuration,
and additive self-driving rows, and it does not cover attaching behaviour to an
existing call path. 426 §11 says so in its own out-of-scope list: "interception
(424(b), deliberately separate: a layer changes what is composed, never what
observes a call)". §2 below is that half.

**R5. Re-realming another author's component (out of scope in 426, needed by
(b)).** 426 §2.3 keeps the limitation that `isolate k in realm(...)` is declared
in the component source, not in the composition, so a layer cannot re-realm
somebody else's component, and calls moving `isolate` into the composition
document "a plausible follow-on, out of scope". §2.2 below measures what that
costs: it is the reason the only same-key interposition shape the current
language admits is one whose body the reference driver never runs.

### 1.3 Slices for (a)

The slice split for (a) IS 426's S1 through S6, unchanged, and this note does
not restate it. Two small slices are added for the residuals decided above,
both of which ride 426's own slices rather than standing alone.

| slice | content | buildable today | exit test |
|---|---|---|---|
| **A1** (folds into 426 S1) | R2, the `granted` clause: surface, empty default, `requires` subset check, refused in a stack layer. | Yes for the parse and the subset check; the profile wiring waits on 426 S4. | A row whose `requires` names a key outside its `granted` set is refused at resolution naming that key; a stack layer writing `granted` is refused at parse; an empty `granted` on a row that requires nothing admits. |
| **A2** (folds into 426 S2) | R3, placement authority: `place` writable in base and site layers only, refused in a stack layer and in the invocation overlay. | Yes once 426 S1's row table exists; nothing new is needed. | A stack layer containing a `place` clause is refused naming the layer; the same clause in the site layer admits; an invocation overlay attempting to set a process or backend is refused as structure. |

---

## 2. Gap (b): interception with declared reach

### 2.1 The question, split so it can be disagreed with

The gap in one sentence: a DSH plugin registers on `tools/pre-execute` and
thereby observes every tool call, which by revl's lights is an undeclared
authority grab, and revl offers nothing in its place. Four sub-questions, each
of which an implementer can answer differently:

- **(b1) What is the intercepted object:** a call, an edge, or a key?
- **(b2) Who chooses the interposition:** the interceptor (DSH's registration),
  the consumer, the provider, or the composition?
- **(b3) What may an interceptor DO:** observe, decide, rewrite?
- **(b4) What bounds its authority, and which rule checks the bound?**

### 2.2 What exists today, measured

Three facts, all checked against `origin/main` at `83cf0e9d` rather than
recalled.

**The word `intercept` is already taken, and means something else.**
`intercept k with { ... }` attaches static metadata to a REQUIRED key (Def. 30,
`docs/design-v2-realms.md`), it applies to required keys only
(`lower.py:8625` refuses it on a provision), and the metadata is static
literals. It is a coeffect annotation, not behaviour. Whatever (b) builds must
not be spelled `intercept`; this note uses `seam`.

**A wrapper on a DISTINCT key compiles today.** The observing wrapper below is
a complete program that the current compiler admits:

```revl
service Db {
  emission[wire, audit, inner_db] fn execute(q: Str) -> Str
}

service Audit {
  emission[log_line] fn record(line: Str)
}

extern emission fn wire(q: Str) -> Str = @py { return "row" }
extern emission fn log_line(s: Str) = @py { pass }

component Inner provides inner_db: Db {
  provide inner_db { fn execute(q) = emit wire(q) }
}

component AuditSink provides audit: Audit {
  provide audit { fn record(line) = emit log_line(line) }
}

component Seam requires inner_db: Db, audit: Audit provides db: Db {
  provide db {
    fn execute(q) {
      let r = emit inner_db.execute(q)
      emit audit.record(q)
      return r
    }
  }
}

component App requires db: Db {
  emit db.execute("select 1")
}
```

The cost is in the first two lines of `Inner`: the provider had to be re-keyed
from `db` to `inner_db` IN ITS OWN SOURCE for the seam to sit in front of it.
That is 424(b)'s "a third party has no place to stand", made exact. Inserting a
wrapper is a source edit to a component the third party does not own, which is
a fork by another name.

**The SAME-key shape also compiles, and its body never runs.** Interposing
without re-keying is expressible through the item-162 multi-realm bind used as
a one-element route: the inner provider writes `isolate db in realm("inner")`
and the seam writes `isolate db in realms("inner")`, requiring `db` in the
inner realm and providing `db` in the parent realm. Consumers are untouched and
G2 holds per `(key, realm)`. It compiles.

It does not run. `run.py:747` is

```python
if comp.get("routes"):
    self._install_router(name, comp)
    continue
```

so a component carrying a `routes` entry is never plugged as a fiber at all:
the driver installs a `_Router` proxy under the key instead (`_install_router`,
`run.py:899`), and `docs/router.md` states the reason plainly, that a routed
require has no single-realm provider so an emitted body would sit PENDING
forever. The consequence for (b) is that **the one interposition shape the
language admits without a source edit is one whose provide body, which is the
whole interception, is silently discarded by the reference driver.** It
compiles, it admits, it passes G4, and the audit call never happens. Read from
source and not executed here (this environment has no cordis-py runtime);
slice B1 below makes it an executed test.

**G4 refuses the naive observing wrapper, and the refusal names the crux.**
Before the `emission[...]` list above was widened, the same program was refused
with:

```text
`Db.execute` is declared `emission[wire]`, but this implementation emits
through `audit`, `inner_db` (reaching `inner_db.execute`, `audit.record`)
  a capability-scoped emission bounds *where* a provider may cross the
  boundary - widen the declaration to `emission[wire, audit, inner_db] fn
  execute(...)` in service `Db`, or route this emission through a declared
  capability (G4)
```

Two facts fall out of that message and they are the whole of (b4):

1. **A seam cannot observe with effect unless the wrapped SERVICE declares the
   bound.** G4 is one-directional by design (`docs/syntax-2.0.md` §4b.1: a
   provider may be purer than declared, never less pure). So the authority to
   log a call is minted by whoever wrote the `service`, which in a layered world
   is the wrong owner. 426 §8.3 already made exactly this argument for `reach`
   ("the person deciding what `pg_connect` may talk to is the operator
   assembling the composition, not the third party who wrote the extern") and
   moved that bound onto the composition.
2. **Interposition renames the capability.** Capabilities are requirement KEYS
   (`docs/capabilities.md` §2: "Requirement keys, not service names"), so the
   seam reaches `inner_db` where the provider reached `wire`. Every consumer's
   capability set changes when a seam is inserted. The capability namespace is
   also realm-blind, so in the same-key shape the seam's inner and outer edges
   carry the same capability name and are indistinguishable in a capability set.

### 2.3 The decisions

**D-424b.1 The unit is the EDGE, spelled as a provision claim.** A seam
addresses `(key, realm)`, exactly as 426 Decision 2 addresses a row. This
answers (b1) and it buys the DSH waterfall property from a landed rule instead
of from a registry: G2 gives at most one provider per `(key, realm)`, so
wrapping the provider wraps every consumer of that key, no enumeration is
needed, and a consumer added later is covered by construction. "Observe every
call to the tool key" is one seam, not a subscription.

**D-424b.2 The composition chooses; a seam never registers itself.** A seam is
a row in the composition document (426 Decision 8):

```revl sketch
seam @audit_db on key("db") observe with @audit_observer through audit
```

There is no hook table, so there is nothing to grab, and this is the direct
answer to 424(b)'s "undeclared authority grab": the authority is not the
seam's to take, it is the composition's to place. It answers (b2). It also
means a seam is subject to every rule 426 already wrote for rows: it has a
label scoped to its origin, it is addressable, it appears in the ROWS block of
the panel, and a stack layer that places one is non-first-party (426 §4.1).

**D-424b.3 A seam is a SYNTHESIZED forwarding provider.** The seam author
writes an observer, never a forwarder. The forwarder is derived from the
service declaration by the same synthesis 426 §3.2 uses for `configure`
(a config row is synthesized from the service declaration, the current
constants and the overrides) and that item 60's `revl test --mock-requires`
already performs. Deriving it rather than writing it removes the whole
"middleware forgot to call next()" failure class that a waterfall has, because
there is no `next()` to forget.

**D-424b.4 Three kinds, and `rewrite` is REFUSED.** This answers (b3).

| kind | the observer's signature | effect on the call |
|---|---|---|
| `observe` | `fn saw(call: Untrusted[Value])` | none. The forwarder calls the inner method and returns its result unchanged. |
| `decide` | `fn allow(call: Untrusted[Value]) -> Decision` where `Decision = Allow \| Deny(Str)` | a `Deny` suppresses the call and becomes the method's `Err`. Admitted only for a method returning `Result[T, E]`; on any other method the seam is refused at admission. |
| `rewrite` | none | **refused. There is no spelling.** |

The argument for refusing `rewrite`, which is the load-bearing one: rewriting
arguments between the point where a call is DESCRIBED and the point where it
EXECUTES is precisely roadmap 427 F2, a HIGH finding still NOT YET FIXED, where
"the ticket, the ledger and the distilled rule shape key all read
`http_post(host="api.stripe.com/SEKRIT-CANARY-APV")` while the host body posted
to `attacker.example`". A construct whose stated purpose is first-class
argument substitution, shipped next to an approval ticket that is computed
upstream of it, reintroduces that finding as a feature. If rewrite is ever
wanted, its precondition is that the ticket, `bind_resource_scope` and the
distilled rule key be computed AFTER the seam chain rather than before, and
that is a change to item 251's machinery, not to this one.

**D-424b.5 The bound is declared by the COMPOSITION, checked by G4 against the
SEAM.** A seam row carries `through <cap>, ...`, and the seam's synthesized
provide bodies are checked against that set rather than against the wrapped
service's declaration. This is the one rule change (b) needs and §2.4 states
what it costs. It answers (b4), and it is the same move 426 §8.3 already made
for `reach`: the authority bound belongs on the object the human approves.

**D-424b.6 Capability attribution through a seam is TRANSITIVE, not local.**
The capability set a consumer of key `k` reaches through a seam is (what the
wrapped provider reached) union (the seam's declared `through` set), and NOT
`{inner_key}`. Without this, §2.2 fact 2 bites: inserting a seam renames every
capability downstream and 426 §8's panel reads a seam insertion as a full
authority turnover, which is the same failure mode 426 §8.2 fixed for component
renames by re-keying tokens on row labels. The seam's own contribution appears
as one new token, `seam:@<label>:<key>`, so the panel shows exactly what was
added and nothing else moves. This is real new work in the capability fold and
is why slice B2 is not buildable today.

**D-424b.7 The observed record is `Untrusted[Value]`, unconditionally.** The
uniform observer surface is a `Value` (`docs/stdlib-value.md`), which is what
lets one observer serve services of different shapes without generics. But
funnelling typed arguments into a dynamic `Value` is exactly a laundering step:
a tainted `Str` argument would arrive at the observer as an untainted `Value`.
The rule is therefore the fail-closed join and not the computed one: the record
is `Untrusted` whether or not any argument was. An observer that sends the
record outbound is then refused by taint (item 249) unless the composition
endorses it explicitly, which is a visible act. Item 424's own closing note
calls taint the single largest asymmetry in revl's favour over DSH; a seam that
silently erased it would spend that asymmetry on the first feature built with
it.

**D-424b.8 The observer never receives the inner handle.** The synthesized
forwarder holds it. So a seam can SUPPRESS a call (`decide`) but can never MINT
one, and it cannot reuse the provider's authority by calling the provider extra
times or with other arguments. Suppression is itself an authority, and it is
exactly what the `decide` kind declares; that is the point of declaring the kind
on the row.

**D-424b.9 Non-first-party seams inherit 426 Decision 6 unchanged.** A seam
shipped by a stack layer compiles under the item-329 untrusted-author profile
like any other non-first-party row, so it declares no `extern` and reaches the
world only through externs the project itself declared. This is what makes
D-424b.5's minted capability safe to mint: the composition grants a seam a
capability NAME, and the only bodies behind that name are the project's own.

### 2.4 What D-424b.5 costs, stated plainly

Today, calling a plain `fn` service method carries a transitive guarantee: by
G4 no provider of it emits, so the call is effect-free all the way down. Under
D-424b.5 that guarantee weakens to "effect-free except through the capabilities
the composition declared at this edge". That is a real loss of local reasoning
and I am proposing it deliberately rather than hiding it. Three compensations:

1. the widening is written in the composition the operator owns, never by the
   seam author and never by the service author;
2. it is per-edge, so it does not travel to any other consumer of the same
   service;
3. it emits a `seam:` token, so 426 §8.5's fail-closed headline cannot print
   `clean` across a seam insertion, and the operator is shown the widening
   before applying it.

**The fallback, if the architect rejects the widening.** Restrict seams to
methods that ALREADY declare an emission bound, and require the seam's `through`
set to be a subset of that bound. That needs no rule change at all: G4 checks
the seam exactly as it checks any provider today, and slice B2 gets smaller.
What it costs is observation of plain `fn` operations, which becomes impossible
without a source edit to the service. For the harness's actual case that
fallback is nearly free, because DSH's `tools/pre-execute` observes TOOL calls
and every tool call is an emission, so the harness's F13 hook story fits inside
the fallback. The general affordance does not. **This is the one decision in
this note I would rather the architect took than took from me**, because it is
a change to who may mint an authority, and `docs/syntax-2.0.md` §4b.1 currently
gives that to the service author alone.

### 2.5 Adversarial pass

**Two seams on one edge reintroduce DSH's ordering problem.** They do not,
because 426 Decision 4 already answers it and applies verbatim: two stack layers
placing a seam on the same edge REFUSE, and only the operator's site layer
resolves, by naming both sides and therefore by naming the order. No implicit
priority, no registration order, no "later entry wins". This is the single
biggest behavioural difference from DSH's waterfall, where the stack order IS
the resolution order and a reordering silently changes behaviour.

**A seam chain could cycle.** It cannot silently: a seam is a row that requires
the key it provides in an inner realm, so a chain is a path in the wiring graph
and G3 refuses a cycle with its existing message. Nothing new is needed.

**A seam is a timing oracle.** True and unfixed: an `observe` seam that only
counts calls still leaks through latency, and no token expresses that. It
belongs in 426 §8.7's BLIND SPOTS block, which is printed always, including on a
clean verdict, for exactly this class of thing.

**The same-token residual.** A seam that reaches an existing capability with a
new argument produces no new token, which is 426 §9.1's named residual (`fs`
already held, a new reacher, no new token) and is closed by item 294's
parameterized capabilities, not here.

**The implementation must not ride `routes`.** The shape measured in §2.2 is a
trap: it compiles, admits, and drops the body. A seam implementation that reused
the multi-realm bind would inherit that. B1's exit test pins the current
behaviour so that the day it changes, something says so.

### 2.6 Slices for (b)

| slice | content | buildable today | exit test |
|---|---|---|---|
| **B1** | No language change. Document the distinct-key wrapper of §2.2 as the sanctioned interposition pattern, with its cost stated (the provider must be re-keyed in its own source), and pin the `routes` hole. | **Yes, today.** Both programs in §2.2 compile against `origin/main`. | A lifecycle test in which a distinct-key wrapper observes a call and the observation is recorded; plus a test asserting that a `routes`-carrying component's provide body is NOT executed by the driver, referencing `run.py:747`, so the hole is a pinned fact rather than a comment. |
| **B2** | `seam` as a composition row: D-424b.1, .2, .3, .8, .9. The synthesized forwarder, the `observe` kind, the label, the panel ROWS line. | **No.** Needs 426 S1 (the row table) and S2 (the fold): a seam has no addressable object before rows exist, and no conflict rule before the fold exists. | Placing a seam on `key("db")` leaves every consumer's source unchanged and every consumer resolves the seam (G2); the observer sees one record per call in order; removing the seam restores a byte-identical manifest; two stack layers seaming one edge refuse, naming both, and permuting the layer list changes only message order; the site layer resolves the pair by naming the order. |
| **B3** | D-424b.5 (the composition-declared `through` bound), D-424b.6 (transitive attribution), D-424b.7 (the `Untrusted[Value]` record), and the `seam:` token. | **No.** Needs B2 and 426 S5 (the panel). D-424b.6 is new work in the capability fold. | A seam emitting outside its `through` set is refused naming the capability and the seam row; inserting a seam produces exactly one added token (`seam:@label:key`) and no other capability moves for any consumer; the panel cannot print `clean` across the insertion; an observer sending the record outbound is refused by taint with no endorse, and admits with one. |
| **B4** | D-424b.4's `decide` kind. | **No.** Needs B2. | A `decide` seam on a method that does not return `Result` is refused at admission naming the method; a `Deny` at runtime lands the method's `Err` with the seam row named in the why-trace and the inner provider never called; `rewrite` has no spelling anywhere in the grammar. |

---

## 3. Gap (c): typed remote client generation

### 3.1 The question, and the three answers revl already has

424(c) says the decorator codegen is redundant because revl already generates
typed boundaries from `service` declarations, but that the CLIENT face is
missing. That is right about the codegen and understates the question, because
revl has three DIFFERENT existing answers to "a call executes somewhere else"
and DSH's `typert` is none of them:

| existing shape | who owns the callee | what is checked | where |
|---|---|---|---|
| the placement seam | me. One composition split across processes. | everything. The proxy and stub are generated from my own manifest; G2 holds across the seam. | `docs/interop-bridge.md` §3, `docs/network-path.md` |
| the federation contract | a sovereign peer. | a pinned CONSUMER SURFACE, checked with `revl contract export` and `revl contract check`. | `docs/federation.md` |
| an `extern` | nobody. It is an opaque host body. | its declared class and reach only. `revl import openapi` generates exactly this: a service, an extern per operation, and a provider component. | `docs/import-openapi.md` |

DSH's `typert` is a fourth: a typed client to a service someone else runs, with
no shared manifest and no pinned mutual contract. So the question is not "can
revl generate a client", it is:

**(c1)** Is a remote provider a ROW, a PEER, or an EXTERN?
**(c2)** What effect class does a remote call carry, and what bounds it?
**(c3)** Is a transport failure a value or a fault?
**(c4)** What is `@RemoteScope`?
**(c5)** What generates the client when the CONSUMER is not revl?

### 3.2 The decisions

**D-424c.1 A remote provider is a ROW whose provider is synthesized.** This
answers (c1).

```revl sketch
remote @billing provides billing: Billing
  at host("billing.internal:8443")
  through billing_wire
```

The wiring is local, so every consumer keeps `requires billing: Billing` and
G2, G3 and G4 are unchanged; remoteness is an ADMISSION fact (a reach, a
capability, a failure mode, a taint qualifier), never a wiring fact. The
rejected alternative is making remoteness a wiring concept, under which every
consumer's source names the transport and bringing a provider back in-process
is a source edit across the composition. revl already made this choice once and
this is the same choice restated: `docs/interop-bridge.md` §3 says which
transport a seam uses "is manifest data, not source text". (c) applies that
rule to a callee that is not in my manifest at all.

**D-424c.2 Remotable requires a declared bound, and G4 already says so.** Every
method of a remotable service must be declared `emission[c]` for some capability
(or `async`); a plain `fn` service is not remotable and the refusal names the
method. This is not a new rule, it is G4 read at the client: a network call is a
boundary crossing, and a provider may be purer than declared but never less
pure. The generated client crosses through ONE extern per service rather than
one per method, so the capability is a single name and the reach is a single
bound on the row, in the shape 426 §8.3 defined. This answers (c2).

**D-424c.3 A transport failure is a WITHDRAWAL by default and a value by
opt-in.** This answers (c3), and it is the decision most likely to be argued
with, so here is the reasoning. revl already has a settled answer for a remote
peer that goes away: peer death is provider withdrawal, R2/R3, the consumer
deactivates reactively and replays its inverses LIFO, and on a network seam a
breached deadline withdraws too because "a wedged remote provider is, to a
consumer, indistinguishable from a dead one" (`docs/network-path.md`). A
generated client that invented a second failure channel would put two answers to
one question in the language. So the default is `on_failure(withdraw)`, reusing
the semantics the bridges already implement and the suite already tests, and a
service that wants to handle failure in-band opts in with
`on_failure(result)`, which is admitted only if every method returns
`Result[T, E]`. Silently swallowing a transport failure has no spelling.

**D-424c.4 `@RemoteScope` is a REALM. There is nothing to build.** DSH's
`@RemoteScope` groups methods into a namespaced remote surface. revl's
equivalent already exists and is checked: a per-peer client is a per-`(key,
realm)` row, and 426 §2.3 already records that realms fall out for free, that
`key("kv", realm: "tenant_a")` is a different address from `key("kv", realm:
"tenant_b")`, and that this needs no new rule. Two peers of the same service
are two rows in two realms and cannot collide. This answers (c4) in full.

**D-424c.5 One wire encoding, and it is already specified.** The generated
client marshals with the canonical value encoding in `docs/interop-bridge.md`
(scalars, `List`, records and `Map` as plain JSON; `Opt[T]` as the bare value or
`null`, never tagged; an ADT or `Result` as `{"$kind": ..., "$value": ...}`), so
a generated client interoperates with the four existing bridges by construction
and no second encoding enters the language. The one divergence that document
already flags, serde's externally-tagged form on the rust side making those
seams rust-to-rust only, is a prerequisite for a rust client target and is
called out there as cheaper to settle once than twice. That is a citation, not a
new decision.

**D-424c.6 The SERVER face is a transport, not a design.** It exists in two
transports already: `revl serve --mcp` serves a composition's own provided
operations as tools named `<prefix>.<key>.<op>` (`src/revl/cli/parser.py:745`),
and the placement bridge serves over UDS and over TCP with mutual TLS. DSH's
`POST /api/<ns>/<method>` is the same projection over HTTP, so `revl serve
--http` adds a transport to an existing projection and decides nothing. It is
also deliberately not redundant with `revl export wit`: WIT is the interface
definition, `serve` is the endpoint, and 424(c) is right that the interface half
is done.

**D-424c.7 For a non-revl consumer, generate from the same projection.** `revl
export client --lang ts|py --service S` emits a typed client over the encoding
of D-424c.5, and it answers (c5). Item 338's landed contract governs what the
generated client may say about itself: 338 fixed its own overclaim with an
ASYMMETRIC contract where a refusal is authoritative and fail-closed while an
admission is a compile-time judgment scoped to `gate_version().frontier` and not
runtime confinement, with frontier promoted to a first-class contract field. A
generated client is a consumer-facing artifact in exactly that sense, so it
carries the frontier and it does not carry a safety claim.

**D-424c.8 A generated client does not re-admit the callee, and must not imply
it does.** Item 337's seam invariant requires that the RECEIVER derive both gate
inputs from independently held state and re-compile from its own source, and its
adversarial review named the failure mode when that does not hold:
admission-theater, where a sender controlling both gate inputs picks the
question. A client sits on the SENDING side and holds no gate over the callee.
So the honest statement, which the generated code and its docs must both make,
is that the client is typed and bounded LOCALLY (its reach, its capability, its
taint, its failure mode) and says nothing whatever about what the callee runs.
If both sides are revl and both want a mutual guarantee, that is `revl contract
export` and `revl contract check`, or 337's seam, and not the client. **No
"verified remote" badge, no green checkmark on the peer.**

**D-424c.9 Every value a generated client returns is `Untrusted[T]`.** A remote
peer is not my trust domain, and 337 treats each tier boundary as its own
admission domain for that reason. Taint (item 249) is the property 424 itself
names as revl's single largest asymmetry over DSH, and a generated client is the
one construct that could quietly become the largest hole in it, because a client
looks exactly like a local provider at every call site. Tainting the results is
what keeps a remote value from reaching an outbound send invisibly.

**D-424c.10 A reach bound is derived from host and port only.** Never from a URL
containing userinfo. This is roadmap 421 F4's shape (a credential riding into a
capability spelling) and the generated reach is a capability spelling. Stated
here because a client generator is exactly where a URL gets turned into a bound.

### 3.3 Slices for (c)

| slice | content | buildable today | exit test |
|---|---|---|---|
| **C1** | D-424c.6 and D-424c.7 as tooling only: `revl export client --lang ts` over the canonical encoding, and `revl serve --http` as the matching server face. No language change. | **Yes, today.** Both are projections of an IR that is already projected this way in another transport. | A generated ts client calls a `revl serve --http` face of the same composition and round-trips a record, an `Opt`, a `Result` and a user ADT byte-identically to the placement bridge's encoding; the generated client carries the frontier field and no safety claim; a service with a method the projection cannot express is refused at generation naming the method. |
| **C2** | D-424c.1 through D-424c.4: the `remote` row, the G4 admissibility check, `on_failure`, per-realm peers. | **No.** Needs 426 S1's row table, since a `remote` is a row and there is no row table yet. | Every consumer of a remoted key compiles unchanged and resolves one provider; a plain `fn` service is refused as unremotable naming the method; two peers of one service in two realms admit and do not collide; a transport failure withdraws the provider and the consumer deactivates reactively (the R2/R3 path the bridges already test); `on_failure(result)` on a non-`Result` method is refused. |
| **C3** | D-424c.9 and D-424c.10: taint on every returned value, reach derived from host and port. | **No.** Needs C2. | A client result flowing into an outbound emission is refused without an `endorse` and admits with one; a peer URL carrying userinfo produces a reach bound with the credential absent, and the credential appears in no capability spelling, no ticket and no WAL record. |

---

## 4. The result the three gaps share

All three answers are the same primitive, and this is worth stating because it
changes the cost estimate.

| construct | synthesized from | status |
|---|---|---|
| a config row (426 §3.2's `configure`) | the service declaration, the current constants, the overrides | designed, 426 S2 |
| a mock provider (`revl test --mock-requires`, item 60) | the service declaration alone | **shipped** |
| a seam forwarder (D-424b.3) | the service declaration plus the observer's kind | designed here, B2 |
| a remote client (D-424c.1) | the service declaration plus the peer address | designed here, C2 |

So B2 and C2 are not two compiler projects. They are two callers of one
`synthesize_provider(service_decl, kind, params)` function plus 426 S1's row
table, and one of the four kinds is already in the tree. The synthesizer is the
thing to build once and build well; the three kinds are then small.

This also explains why (b) and (c) both wait on 426 S1 and neither waits on
anything else in 426. A seam and a client are both ROWS, and a row is the object
426 S1 creates.

---

## 5. Dependencies

| dependency | needed by | status |
|---|---|---|
| **426 S1** (the row table) | B2, C2, A1, A2 | designed, buildable today, depends on nothing |
| **426 S2** (the fold) | B2 (peer conflict on one edge), A2 | designed, follows S1 |
| **426 S5** (the authority panel) | B3 (the `seam:` token) | waits on 426 S4 and 428 F3 |
| **426 Decision 6 / 425 F1** | B2's non-first-party seams (D-424b.9) | 425 F1 NOT on main; 426 S4 waits on the decision, not the branch |
| **item 337 seam invariant** | R3 (placement authority), D-424c.8 | design done; cited, not extended |
| **item 338 asymmetric contract** | D-424c.7 (what a generated client may claim) | design done; cited, not extended |
| **item 249 taint** | D-424b.7, D-424c.9 | complete. Both decisions spend it rather than working around it |
| **item 294 parameterized capabilities** | the same-token residual in §2.5 | open; a named blind spot, not a blocker |
| **item 162 / 161 router** | nothing here builds on it, and B1 pins why | landed; `run.py:747` is the hole B1 records |
| **item 350 environment binding** | nothing in this note | open. Noted because a `remote` row's address is exactly the class of value 350 says is read before the composition exists; if 350's `boot` component lands, a peer address should come through it rather than through a second mechanism |

---

## 6. Where this note argues with something, and where it does not

**It does not contradict item 426 anywhere.** §1 is a citation map. R2 completes
426 §9.3 Part 2 with a surface that decision requires and does not spell; R3
decides a question 426 left open, and decides it by citing 337 rather than by
inventing a rule.

**It argues with one thing, explicitly: who may mint an emission bound.**
`docs/syntax-2.0.md` §4b.1 gives that to the service author alone, and
D-424b.5 proposes that the composition may widen it at one edge for a seam.
That is a real change to a landed rule, its cost is a weakening of transitive
purity for plain `fn` methods across a seamed edge, and §2.4 gives the
compensations and the no-rule-change fallback. It is flagged as the one decision
in this note that the architect should take rather than accept.

**It records one thing 424 itself got slightly wrong.** 424(a) says the
difference from DSH is "cost, not safety" and stops there. 426 §5.2 shows the
cost is not uniform, because activation of a `replace` or `remove` is blocked by
G7 rather than by effort. The sentence should read: the ADMISSION difference is
cost, the ACTIVATION difference is a guarantee revl keeps and DSH does not have.
