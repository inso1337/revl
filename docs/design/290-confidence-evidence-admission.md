# 290: Confidence/evidence admission policy (`revl policy evaluate`)

Status: DESIGN (implementation pending)
Roadmap: item 290 (external proposal, one line; part of the 243-261
product-vision triage). Reconciles with 33 (boundary policy), 45 (quarantine
tier), 246 (approval gate), 251 (approval distillation), 293 (evidence-carrying
registry), 294/309 (declaration-strength registers), 329/411 (trust boundary).

## 1. The proposal, and what it must become

The external proposal is one line: admission should be able to weigh EVIDENCE
and CONFIDENCE, not just a hard allow/deny. Taken literally, that sentence asks
revl to do the one thing it must never do: turn the gate into a heuristic.
revl's whole discipline is that a gate admits or refuses on facts it can name;
there is no "probably safe" verdict anywhere in the system, and item 290 must
not introduce one.

Taken seriously instead of literally, the proposal names a real gap. The
boundary policy (item 33) constrains WHAT a component may reach. Nothing in the
policy language constrains HOW WELL VERIFIED a component must be before it is
admitted at all. Meanwhile item 293 just landed exactly the missing input: a
registry component now carries a machine-verifiable evidence bundle
(attestation, gauntlet dossier, fault-sweep dossier, inverse-roundtrip dossier,
capability surface, provenance), and `revl_resolve` already grades that bundle
into a deterministic quality assessment (`registry.assess_evidence`) to RANK
interface-compatible candidates. Ranking is the discovery side. Item 290 is the
admission side of the same evidence: an operator writes, in the item-33 policy
file, a hard threshold that a component's evidence must clear before the
composition admits it.

So the definition this design commits to:

> A confidence/evidence admission policy is a new rule kind in the item-33
> boundary policy that admits or refuses a component based on hard thresholds
> over the objective facts in its item-293 evidence bundle and the
> declaration-strength registers of its claims. `revl policy evaluate` is the
> dry-run verb that reports, per rule, which thresholds pass and fail and why,
> naming the recorded fact against the required threshold.

Not a parallel policy engine. Not a score. One new rule family in the existing
`Policy`, evaluated by the existing `policy.evaluate` walk, refusing with the
existing `Violation` + why-trace machinery, surfaced through the existing gate
(`revl audit --policy`, admission `enforce`) plus one new explain verb.

## 2. The hard line: confidence is a threshold, not a probability

This is the load-bearing decision of the whole design, so it comes first.

Every fact the rule language can threshold is OBJECTIVE and RECORDED:

- a fault sweep passed 8 of 12 injected steps: two integers in a dossier;
- an attestation is cryptographically valid against the rebuilt IR, or it is
  not: a signature check with an operator-held key;
- an inverse round-trip passed or failed: a recorded verdict;
- a gauntlet run ended `admissible` or it did not;
- a publisher is in the operator's trust set or it is not (trust is supplied by
  the operator, never self-asserted: `registry._publisher_status`);
- a declaration carries the `declared` or `keyed` register (the item-44/309
  honesty ledger, plus 440's `read`; section 3.2 adopts 309's order).

A rule is a predicate over these facts. `component Csv* requires evidence
[fault-sweep 12/12, attestation valid]` either holds or does not hold; the
component is admitted or refused; the refusal names the failing fact. What is
new in 290 is only the INPUT (evidence facts were previously invisible to the
policy) and the operator's ability to set the threshold. The gate's logic stays
two-valued.

What the design REFUSES, permanently, not as a deferred slice:

- No numeric confidence. There is no `confidence 0.8`, no weight, no score, no
  combining function in the grammar. "Confidence" in the item title is realized
  as the operator's chosen evidence threshold, nothing else.
- No ranking at the gate. `revl_resolve` may ORDER candidates by evidence
  quality (item 293's rank_key), because choosing among admissible options is a
  preference. Admission never orders; it admits or refuses each component
  independently.
- No partial credit across facets. Strong attestation does not compensate for a
  failed sweep. Every clause in a rule must hold; a rule is a conjunction, and
  rules compose conjunctively with the rest of the policy (section 5).
- Missing evidence never passes. A facet a rule names that is `unavailable`
  fails that clause, the same way an unnameable `*` reach never satisfies a
  closed allow-list (`policy._allowed`). Fail-closed is not negotiable.

One honest caveat, stated here the way item 33 states its G8 caveat: an
evidence threshold is a hard predicate over what the evidence RECORDS, and most
of the record is author-produced. Passing `fault-sweep 12/12` proves the
dossier says 12/12, not that the sweep ran as claimed, unless the bundle is
rooted in something the operator can verify. Section 6 makes that trust
boundary a first-class part of the rule language rather than a footnote.

## 3. The rule grammar: `kind: evidence` over the item-33 policy

### 3.1 Facet vocabulary (closed)

The facet names and statuses are taken VERBATIM from the shipped item-293
assessment (`registry.assess_evidence`); 290 introduces no new grading. Each
facet's statuses already form a total order (the `_RANK` tables in
`registry.py`), which is exactly what makes a hard threshold meaningful:
"at least this status".

| facet             | statuses, strongest first                  | threshold forms            |
|-------------------|--------------------------------------------|----------------------------|
| fault-sweep       | full > partial > unavailable               | `full`, `P/S` (see below)  |
| attestation       | valid > present > unavailable > invalid    | `valid`, `present`         |
| inverse-roundtrip | pass > fail > unavailable                  | `pass`                     |
| gauntlet          | admissible > present > unavailable         | `admissible`, `present`    |
| publisher         | trusted > present > unavailable            | `trusted`                  |
| capabilities      | present > unavailable                      | `present`                  |

Notes:

- `attestation invalid` ranks BELOW `unavailable` (a tampered attestation is
  worse than none, exactly as the resolve ranks it), so no satisfiable
  threshold ever admits an invalid attestation.
- The fault-sweep numeric form `fault-sweep 12/12` holds iff the dossier's
  recorded coverage satisfies `steps >= 12 and passed == steps and
  unreachable == 0`: at least that many steps were swept, every swept step
  passed, and no step sat beyond the scheme's reach. The numerator must equal
  the denominator: `fault-sweep 8/12` is a `PolicyError` at parse time. The
  only semantics on offer is all-passed (section 2 forbids partial credit),
  so a numerator below the denominator can only mislead: an operator writing
  `8/12` expects partial credit the design refuses, and under all-passed
  semantics the `8` would be dead weight. `fault-sweep full` holds iff
  `passed == steps > 0 and unreachable == 0`. The `unreachable == 0` check is
  290's addition on top of 293's grading: `_sweep_status` grades `full` from
  `status == passed and passed == steps` alone, so a component with 12
  sweepable steps and 8 the scheme cannot address grades `full` at 12/12
  while nearly half its effects were never exercised. The gate reads the
  recorded `unreachable` count directly (an integer comparison over recorded
  counts, like the rest of the form; no new grading logic) and refuses to
  call that `full`. A `passed` dossier with zero steps is honest but
  weightless (`partial`, per `_sweep_status`) and satisfies neither form. The
  numeric form exists because "full" floats with the program: an operator
  pinning a floor wants "a sweep of at least this size, fully passed". One
  honest sentence to keep expectations straight: the step count is
  author-controlled, so twelve trivial reversible effects yield an honest
  12/12; the numeric floor buys a size proxy, not rigor. It keeps out toy
  sweeps; it does not certify sweep quality.
- `publisher trusted` is graded against the trust set the EVALUATION supplies
  (section 4), mirroring `resolve(trusted_publishers=...)`. A component cannot
  self-assert trust.

### 3.2 Line DSL

Extending the item-33 DSL (docs/boundary-policy.md), same subjects as the reach
rules plus a capability-scoped form:

```
component <glob>          requires evidence [<facet> <threshold>, ...]
component registry:<glob> requires evidence [<facet> <threshold>, ...]
realm <name>              requires evidence [<facet> <threshold>, ...]
mcp                       requires evidence [<facet> <threshold>, ...]
capability <glob>         requires evidence [<facet> <threshold>, ...]
capability <glob>         requires register <level>
<any evidence rule> ... self-attested     # unrooted acknowledgment, section 6.3
evidence-root: local                      # policy-level acknowledgment, section 6.3
```

Examples:

```
# every registry-resolved component must be fully swept and attested;
# first-party bare-source components are outside this rule by construction
# (origin scoping, below)
component registry:* requires evidence [attestation valid, fault-sweep full]

# the agent sandbox: agent-admitted code must have survived the gauntlet
# (satisfiable for drafts from slice 1 via the session gauntlet dossier,
# section 4)
mcp                requires evidence [gauntlet admissible]

# anything reaching payments must come from a trusted publisher, attested
capability payments.* requires evidence [publisher trusted, attestation valid]

# a witnessed inverse behind inventory.* may not be a bare trust-me claim
# (floors above `declared` land with 309's ledger; see below)
capability inventory.* requires register keyed
```

Subject semantics follow the shipped rules exactly: `component <glob>` selects
by `fnmatchcase` over the component name, `realm` by isolate membership
(`component_realms`), `mcp` by MCP-session admission (the same
`mcp_components` set the sandbox uses). The new `capability <glob>` subject
selects every component whose G8 reach (`component_reach`) includes a token
matching the glob: "whatever touches payments must clear this bar". A
component selected by several evidence rules must satisfy ALL of them
(conjunction; nothing widens).

**Origin scoping.** The `registry:` prefix scopes a component rule by
ADMISSION ORIGIN, and it exists because the unscoped flagship rule is
unwritable in practice: `policy.evaluate` walks EVERY component, a real
composition always contains first-party components with no registry bundle,
so `component * requires evidence [...]` refuses the operator's own main
component. The tempting remedies are worse. Scoping by name glob
(`component vendored-*`) or an explicit exemption form
(`component local-* exempt evidence`) INVERTS fail-closed: a vendored
component renamed to dodge the glob silently escapes the bar, and the safe
sentence "every component except these first-party ones" is inexpressible by
name. Origin cannot be dodged by renaming, because it is recorded by the
admission path itself, never asserted by the component. The audit graph
therefore carries each component's admission origin (`registry` for
registry-resolved admission, `source` for bare-source; MCP-session
membership is already its own set), populated at admission time, and
`component registry:<glob>` selects by origin AND `fnmatchcase` over the
name. Bare `component <glob>` keeps its shipped meaning: name only, any
origin. The exemption form is rejected; the origin-scoped selector is the
design.

`requires register <level>` is the declaration-strength floor over the
item-44/309 honesty ledger. 290 adopts 309's PARTIAL order verbatim, because
309 is the source of the ledger and the two items must not grade the same
declaration differently:

```
declared < keyed < read       (`strong` = any register above `declared`)
```

`declared` is the bare item-44 trust-me register; `keyed` is the
idempotency-key discipline; 440's `read` (`undo pure`) sits above both,
because a call that changes nothing needs no key at all. An earlier draft of
this design used the total order `declared < keyed < sweep-evidenced <
shape-proven`; that is withdrawn. `sweep-evidenced` is not a declaration
register at all but evidence about the component, already thresholdable as
the `fault-sweep` facet; keeping it in the ladder would fork 309's
vocabulary. The floor vocabulary is therefore `declared`, `keyed`, and
`strong` (any register above the trust-me floor). An operator who means
"verified, either way" writes `strong`.

**Item 207 removed `shape-proven` from this section.** It was written here as
a PEER of `keyed` — a static proof over an inverse body — and nothing ever
produced it. Its provenance is an inverse, and every consumer that graded the
register set grades a forward emission, so no declaration could reach it; over
the whole producible vocabulary the `shape-proven` floor was an exact synonym
for `strong`. The spelling is now a parse error naming `strong`. See
`docs/design/207-checkable-extern-body.md`, and §4d of `docs/crash-recovery.md`
for the rule that answers the operator sentence `shape-proven` was reached for
("verified, not merely claimed", for a local mutating inverse): pair this floor
with `requires evidence [inverse-roundtrip pass, attestation valid]`.

The rule selects components reaching the capability and refuses any whose
relevant declarations (today: the witnessed-inverse and idempotence claims
lowered onto that boundary) sit below the floor. Two evaluator rules are
stated here so slice work does not invent them ad hoc:

- **Worst register wins per capability.** When a register rule selects a
  component, EVERY declaration lowered onto a matching reach token must meet
  the floor; the effective register of a capability is the WEAKEST among the
  declarations behind it. One bare `declared` inverse beside three keyed
  ones fails a `keyed` floor. This is the same worst-wins discipline as the
  rest of the policy (section 5).
- **The audit surface.** `component_reach` yields `(token, via, kind)`
  tuples, not declaration objects, so registers are invisible to the walk
  today. The audit graph gains a declaration-register surface: a
  `capability_registers` map from reach token to the register levels of the
  declarations behind that token, alongside `component_reach`, populated at
  lowering time. The rule evaluates against that map, and the why-trace
  names the weakest declaration.

Timing, stated bluntly (as of this note; 309 has since landed the ledger): the
register does not exist in the IR yet. Only `idempotent: true` is recorded per
method; no `keyed` string exists anywhere in `src/revl`, and there is no
`idempotent(key: p)` parse form. A floor above `declared` accepted today would be an unsatisfiable
rule, an unconditional deny wearing a register costume, whose meaning
silently flips the day 309's ledger lands. So slice 1 parses
`requires register declared` only and rejects every higher level at parse
time with a distinct `PolicyError` ("register level `keyed` is not yet
recordable; lands with 309's ledger"): the vocabulary is closed in time as
well as in space. The higher floors ship behind 309's ledger (section 9).

### 3.3 JSON equivalent

Same `Policy`, machine-authored, alongside the existing `components` / `realms`
/ `approvals` keys:

```json
{
  "evidence": [
    {"component": "registry:Csv*",
     "require": {"attestation": "valid", "fault-sweep": "12/12"}},
    {"component": "vendored-*",
     "require": {"fault-sweep": "full"}, "selfAttested": true},
    {"capability": "payments.*",
     "require": {"publisher": "trusted", "attestation": "valid"}},
    {"mcp": true, "require": {"gauntlet": "admissible"}}
  ],
  "registers": [
    {"capability": "inventory.*", "atLeast": "keyed"}
  ],
  "evidenceRoot": "local"
}
```

The `registry:` origin prefix is the same string in JSON; `selfAttested` and
the policy-level `evidenceRoot` are the section-6.3 acknowledgments (the
example shows both forms; a real file needs at most one).

An unknown facet name, status, or register level is a `PolicyError` at parse
time (a closed vocabulary, like every other rule family): a typo must not
become a rule that silently requires nothing. So are the malformed shapes
from sections 3.1 and 3.2: a numeric sweep threshold with numerator below
denominator, and a register floor above `declared` before 309's ledger
lands.

### 3.4 Model changes

`Policy` gains two tuples, defaulting empty so every existing policy file
parses byte-identically and `is_empty()` stays honest:

```python
@dataclass(frozen=True)
class EvidenceRule:
    scope: str                        # "component" | "realm" | "mcp" | "capability"
    selector: str                     # glob / realm name ("" for mcp)
    origin: str | None                # "registry" for origin-scoped rules, else None
    require: tuple                    # ((facet, threshold), ...) conjunction
    self_attested: bool               # explicit unrooted acknowledgment (section 6.3)

@dataclass(frozen=True)
class RegisterRule:
    capability: str                   # glob over capability tokens
    at_least: str                     # "declared" | "keyed" | "strong"
```

`Violation` gains two `kind` values, `evidence` and `register`, beside the
existing `capability | deny | tenant | mcp-sandbox | approval | declassify |
taint-flow`. The why-trace for an evidence violation is a CHAIN: component ->
the failing facet with its recorded fact -> the rule's threshold, so the
refusal reads "component `csv-reader` fault-sweep is 8/12, rule requires
12/12".

## 4. Where the evidence comes from, and evaluation semantics

`policy.evaluate` today takes `(policy, audit, mcp_components)`. Evidence rules
need one more input: the per-component evidence. The signature grows an
optional map, defaulting empty:

```python
evaluate(policy, audit, mcp_components=None,
         evidence=None,        # {component_name: EvidenceBundle}
         trusted_publishers=frozenset(), key=None)
```

Grading reuses `registry.assess_evidence` verbatim: the policy module never
re-derives a facet status; it compares the assessment's statuses (and the raw
sweep coverage for the numeric form) against the rule's thresholds. One
grading path for resolve-ranking and admission-thresholding, by construction.

Where the bundle comes from, per admission path:

- **Registry-resolved component.** A resolve result already carries the
  candidate's evidence facets and the entry's bundle rides with source +
  manifest (`RegistryEntry.evidence_bundle`). Admission of a resolved component
  passes that bundle through. This is the primary path and the reason 290 is
  the natural counterpart of 293: resolve RANKS by evidence, admission
  THRESHOLDS the same evidence.
- **Bare-source component** (local file, agent-generated draft). No bundle:
  every facet is `unavailable`, so a selecting evidence rule refuses. This is
  the correct fail-closed reading, not a bug: an operator who writes "only
  attested, fully-swept components" has refused evidence-less code by
  definition. Compositions with first-party code scope their evidence rules
  by ORIGIN (`component registry:*`, section 3.2), never by name-glob
  exemption, or run producers locally (below).
- **MCP-admitted agent draft.** A draft admitted through the MCP session is
  bare-source, but the session's OWN gauntlet (`mcp/gauntlet.py run()`) has
  just produced a gauntlet dossier live at admission time: operator-run
  evidence that needs no attestation root, already in hand at exactly the
  evaluation site. Slice 1 plumbs that dossier into the evidence map for
  MCP-admitted components, so the recommended item-329 pairing
  `mcp requires evidence [gauntlet admissible]` is satisfiable in slices 1
  and 2 for drafts that survived the gauntlet, instead of refusing every
  draft until slice 3's `--recompute` lands. All other facets stay
  `unavailable` for drafts, as they should.
- **Locally recomputed evidence** (slice 3): `revl policy evaluate
  --recompute` may run the producers the operator already has (gauntlet, fault
  sweep, inverse round-trip) against the component in hand and grade THAT
  dossier. Operator-run evidence needs no attestation root, because the
  operator produced it; the report marks each facet `recomputed` vs
  `published`. Until that slice lands, evidence is whatever the bundle
  supplies.

Evaluation is per-component and pure, like every existing rule family: for
each component in the audit graph, collect the evidence rules that select it
(by name glob, realm, mcp membership, or capability reach), assess its bundle
once, and emit one `Violation` per failing clause. `enforce` refuses on the
first violation exactly as today; nothing about the gate's shape changes.

## 5. Composition with the shipped rule families: nothing widens

The composition rule is the item-33 rule, extended: EVERY selecting constraint
must hold, and the strictest verdict wins.

- An evidence rule composes conjunctively with allow/deny reach rules. A
  component must be within its allow-lists AND outside every deny AND clear
  every selecting evidence threshold. Deny still wins over everything; an
  evidence rule can only REFUSE, never admit a component the reach rules
  refuse, and never substitute for a missing allow.
- With `capability C requires approval` (item 246): independent gates that
  stack. Evidence is about the component's verification quality at admission;
  approval is about a human authorizing a crossing at runtime. Strong evidence
  NEVER waives an approval requirement, and an approval never waives an
  evidence threshold. (The tempting product feature "auto-approve crossings
  from high-evidence components" is exactly the softening section 2 forbids;
  the sanctioned road from repeated approvals to policy is item 251, below.)
- With `quarantine required` (item 45): the ancestor special case. Quarantine
  is "prove yourself in the wasm sandbox before admission", which is an
  evidence requirement with a fixed facet, spelled as a flag before the
  vocabulary existed. It stays as-is (its proof is operator-run in the
  quarantine tier, which is stronger than a published gauntlet dossier); the
  design notes the family resemblance and stops there. A later cleanup may
  re-express it; 290 does not touch it.
- With the taint rules (item 249): orthogonal subjects (data flow vs
  verification quality), no interaction beyond both being able to refuse.
- With 411/329: an evidence rule is a plan-time gate like the sandbox-placement
  cap gate; it composes with (and does not replace) runtime confinement. An
  untrusted-author profile (329) will typically pair `--taint-strict` with
  `mcp requires evidence [gauntlet admissible]`.

Reconciliation with item 251 (approval distillation): 251's contract is that a
distilled rule is "33's policy, never regexes", reviewed once with its blast
radius. Item 290 enlarges the vocabulary 251 can distill into. The ledger
observing an operator repeatedly approving swaps to components that are
attested + fully swept can now offer the typed diff `component
registry:csv-* requires evidence [attestation valid, fault-sweep full]`, with
the same would-have-covered accounting. 290 ships no distillation logic; it ships the
rule kind 251 will target.

Reconciliation with item 293's resolve: unchanged code paths, one new
CONSISTENCY property worth a test: a candidate that would be refused by the
active policy's evidence rules should be flagged in the resolve result (a
`wouldBeRefused: [rule, ...]` marker on the candidate) so an agent does not
pick a top-ranked candidate the gate then bounces. Ranking stays ranking;
the marker is a courtesy prediction computed by the same evaluator.

## 6. The trust boundary: evidence about the evidence

The bundle is author-produced (item 293's design says so explicitly: facets are
verbatim producer output, assembled at publish, never re-derived). An evidence
policy that thresholds author-supplied dossiers is only as good as the
bundle's own root of trust. The adversarial review found the first draft of
this section broken at its root, so it now states the gap first and the
rules second.

### 6.1 Today, the trust root does not root the dossiers

What the shipped item-293 artifacts actually sign and check:

- `attest._body` signs only the IR-level composition facts: kind, version,
  verdict, IR hash, guarantees, timestamp, signer. The fault-sweep,
  gauntlet, and inverse-roundtrip dossiers are SEPARATE, UNSIGNED files
  under `evidence/`; `registry.build_evidence` writes them verbatim side by
  side with the attestation.
- `registry.verify()` checks index/manifest reproducibility only; it never
  opens the evidence files.
- `_sweep_dossier` carries no source or IR hash and no signature; nothing
  ties a dossier to the component it claims to describe.

Consequence: a publisher holding an honest, signed, valid attestation can
hand-write `evidence/fault-sweep.json` with `{status: passed, 12/12}`, or
copy a simpler component's dossier wholesale, and `component registry:Csv*
requires evidence [attestation valid, fault-sweep 12/12]` admits with false
confidence. That is exactly the failure item 290 exists to prevent, and in
the first draft the unrooted-threshold warning stayed silent because the
rule "has" an attestation clause. An attestation that does not COVER the
dossiers must not be allowed to vouch for them.

### 6.2 The fix: bind the dossiers to the signed root

- **Per-facet dossier hashes enter the signed payload.** The attestation
  payload is extended with the sha256 of each evidence dossier the bundle
  publishes (or the binding lives in the provenance document, which already
  carries `sourceSha256` and `manifestSha256` and is the natural home,
  provided the attestation signs it). `attestation valid` then means: the
  signature verifies AND every dossier present in the bundle hashes to its
  signed binding. A bound dossier whose bytes do not match is a tamper and
  grades the attestation `invalid` (below `unavailable`, as always); a
  dossier present in the bundle with no binding in the signed payload is
  merely self-attested and gains nothing from the attestation. This is a
  small item-293 amendment; it lands WITH slice 1 (section 9), because
  290's gate semantics depend on it.
- **Only a covering attestation roots.** An `attestation valid` clause
  silences the unrooted diagnosis (6.3) for exactly the facets whose
  dossiers are bound inside the verified payload, and for no others. Until
  the binding lands, the sweep, gauntlet, and inverse-roundtrip facets are
  ALWAYS self-attested with today's artifacts, so a rule thresholding them
  is unrooted, attestation clause or not.
- **Verified against the rebuilt IR at admission.** Admission-time
  `attestation valid` is verified against the ADMITTED component's REBUILT
  IR: rebuild from the source actually being admitted, then verify the
  signature against that (`attest.verify_attestation`, plus
  `truc reproduce` for artifact-level reproduction). It is never merely
  re-read from the bundle in transit; otherwise a resolved-then-modified
  source rides into the composition on the original, still-valid
  attestation.
- **`valid` needs a key, and fails closed without one.** `assess_evidence`
  without a key can grade an attestation at most `present` (well-formed, not
  verified). A policy demanding `attestation valid` evaluated without a
  verification key REFUSES with a distinct reason ("cannot verify is not
  valid"), mirroring the shipped `verify_required` resolve which refuses to
  run keyless rather than silently downgrading.
- **`publisher trusted` roots nothing by itself.** Membership in the
  operator's trust set says who published, not that the dossiers are
  theirs; it roots dossiers only in combination with a covering valid
  attestation under that publisher's key.

### 6.3 Unrooted thresholds are an error, acknowledged or refused

The first draft made an unrooted threshold a Python `UserWarning`. The
review is right that a warning to stderr in CI trains blindness: it scrolls
past once, then forever. The design replaces it with a named, grep-able
decision:

- A rule that thresholds self-attested facets (per 6.2: any fault-sweep,
  gauntlet, or inverse-roundtrip threshold not covered by a
  binding-verified `attestation valid` clause in the same rule) is a
  `PolicyError` at policy-load time, UNLESS the operator acknowledges it
  explicitly, either per rule:

  ```
  component vendored-* requires evidence [fault-sweep full] self-attested
  ```

  or once for the whole policy (a private registry or local-recompute shop):

  ```
  evidence-root: local
  ```

- The acknowledgment is a one-time named decision a reviewer can grep for
  (`self-attested`, `evidence-root`), not a perpetual warning to tune out.
  The legitimate case from the first draft, an operator thresholding
  evidence they produced themselves, stays fully expressible; it is now
  spelled in the policy instead of implied by warning fatigue.
- Facets that are operator-run at evaluation time need no acknowledgment:
  the MCP session gauntlet dossier (section 4) and slice-3 `--recompute`
  facets are rooted in the operator's own run by construction.
- Any advisory diagnostic that remains, and the acknowledgment itself,
  appear in the `revl policy evaluate` report BODY and in `--json`
  (`"selfAttested": true` per rule), never only on stderr.

### 6.4 The report restates the boundary

Like the item-33 report restates the G8 lying-pure-extern caveat, every
`revl policy evaluate` report states, per facet, which of three standings
its fact has: verified (key checked, dossier hash bound and matching, IR
rebuilt), operator-run at evaluation time, or read from the published
bundle as an acknowledged self-attested claim.

## 7. `revl policy evaluate`: the explain verb

New CLI verb (there is no `policy` verb today; the gate lives in `revl audit
--policy` and admission's `enforce`). The gate answers "admitted or refused,
first violation". The new verb answers "under this policy, which rules select
which components, which clauses pass and fail, and on what facts":

```
revl policy evaluate <policy-file> <program.rvl ...>
    [--component <name>]            # narrow the report
    [--registry <dir> --candidate <name>]   # evaluate a registry entry instead
    [--evidence <dir>]              # bundle dir for a bare-source component
    [--trusted-publisher <id> ...]  # the operator trust set
    [--key <path>]                  # attestation verification key
    [--mcp-scope <name>|*]          # as `revl audit --policy`
    [--json]
```

Output, per component, per selecting rule, EVERY clause (passing and failing),
fact against threshold:

```
component csv-reader
  rule: component registry:* requires evidence [attestation valid, fault-sweep 12/12]
    attestation      valid            (verified, bindings match)   PASS
    fault-sweep      8/12 partial     required 12/12               FAIL
  rule: capability payments.* requires register keyed
    (not selected: csv-reader does not reach payments.*)
  verdict: would be REFUSED (1 failing clause)
```

Contract:

- **Same evaluator as the gate.** The verb calls the same `policy.evaluate`
  with an explain mode that collects every clause verdict instead of only
  violations. There is exactly one place a threshold is compared, so the
  dry-run can never disagree with the gate; a golden test pins that (the verb's
  refused set equals the gate's refusals on the same inputs).
- **Vacuous admission is named, not blurred.** The verdict line
  distinguishes the two ways a component passes: `admitted: no evidence rule
  selects this component` (vacuous, nothing was checked) versus
  `admitted: all N clauses across M selecting rules hold` (checked and
  passed). An operator scanning the report must be able to tell "nothing
  applied" from "everything held" without reading the clause table; JSON
  carries `"selected": false` versus the full clause list.
- **Inert selectors are reported.** Following item 249's
  `_warn_if_taint_rules_are_inert` precedent verbatim: an evidence or
  register rule that selects NO component in the audit graph is reported as
  inert (`component Csv*` never matches a component named `csv-reader`;
  `fnmatchcase` is case-sensitive). The diagnosis appears in the report body
  and in `--json`, so a typo'd selector cannot silently require nothing.
- **Dry-run only.** It never admits, refuses, or mutates; exit 0 when
  everything selected would be admitted, 1 when anything would be refused, 2 on
  a parse/usage error. `revl audit --policy` remains the enforcement surface
  and additionally reports the non-evidence families it already reports; the
  new verb subsumes its reporting for ALL rule families (reach, deny, tenant,
  approval, taint, evidence, register), since "which rules would fire and why"
  is equally useful for the shipped families. `revl audit --policy` stays for
  compatibility and for the admission-shaped one-line verdict.
- **Registry mode.** `--registry --candidate` evaluates a published entry
  (source + manifest + bundle from the entry dir) as if admitted into the
  current composition, which is precisely the "should I take this candidate"
  question an agent asks between resolve and apply.
- **JSON** mirrors the human report: `{component: {rules: [{rule, selected,
  clauses: [{facet, fact, threshold, pass, verified}], verdict}]}}`, with the
  why-trace attached to failing clauses, so the dashboard's "why not admitted"
  view (item 286) can render evidence verdicts beside the reach verdicts with
  no second format.

## 8. Non-goals

- No probability, score, weight, or combining function, ever (section 2).
- No evidence-based auto-approval of item-246 crossings.
- No new grading logic: facet statuses come from `registry.assess_evidence`.
- No new policy engine, file format, or evaluation pass outside
  `policy.evaluate`.
- No re-expression of `quarantine required` (noted as kin, left untouched).
- No transitive/dependency evidence ("my dependencies are all attested"): that
  is 293's trust-graph fold, and it plugs in later as new facets with the same
  threshold discipline.

## 9. Staged plan

**Slice 1: the rule kind, rooted.** `EvidenceRule`/`RegisterRule` in
`policy.py` (DSL + JSON parsing, closed vocabulary, `PolicyError` on unknown
facet/status/level, on numeric sweep thresholds with numerator below
denominator, on register floors above `declared`, and on unacknowledged
unrooted thresholds per section 6.3), the origin-scoped
`component registry:<glob>` selector with admission origin recorded in the
audit graph, evaluation over a supplied `{name: EvidenceBundle}` via
`registry.assess_evidence` plus the raw sweep counts (`passed`, `steps`,
`unreachable`), `evidence`/`register` violations with why-traces,
keyless-`valid` fail-closed, the item-293 amendment binding per-facet
dossier hashes into the signed payload with `attestation valid` verifying
the bindings against the rebuilt IR (section 6.2; the gate's semantics
depend on it, so it is in this slice, not deferred), the MCP session
gauntlet dossier plumbed into the evidence map for MCP-admitted components
(section 4), composition tests with the shipped families. `revl audit
--policy` gains `--evidence`/`--key`/`--trusted-publisher` plumbing so the
gate can actually see bundles.

**Slice 2: `revl policy evaluate`.** The explain mode on `policy.evaluate`
(every clause verdict, pass and fail), the verb with the CLI shape of section
7 including registry mode, JSON output, the gate-agreement golden, the
resolve-side `wouldBeRefused` marker.

**Slice 3: recomputed evidence + register depth.** `--recompute` running local
producers and marking facets `recomputed` vs `published`; the register
floors above `declared` (`keyed`, `strong`) unlocked when
309's ledger actually records those registers in the IR (they stay
parse-rejected until then, per section 3.2); distillation (251) taught to
emit evidence rules stays in 251's own item, unblocked by slice 1.

## 10. Exit tests

1. **High evidence admits; first-party code stays writable.** A
   registry-resolved component whose bundle grades `attestation valid` (real
   key, real signature, every dossier hashing to its signed binding) and
   `fault-sweep 12/12` is admitted under `component registry:* requires
   evidence [attestation valid, fault-sweep full]`; the evaluate report
   shows both clauses PASS with the recorded facts. A first-party
   bare-source component in the SAME composition is not selected by the rule
   and admits; its verdict line reads "admitted: no evidence rule selects
   this component".
2. **Low evidence refuses, naming the threshold.** The same policy over a
   bundle with an 8/12 sweep refuses admission; the violation and the
   `revl policy evaluate` report both carry "fault-sweep 8/12, required full
   (12/12)" and a why-trace chain component -> facet -> rule.
3. **Missing evidence refuses (fail-closed).** A bare-source component with no
   bundle is refused by any selecting evidence rule; every facet reports
   `unavailable`.
4. **Self-attested evidence refuses under require-attestation.** A bundle with
   a full sweep but no attestation fails `[attestation valid, ...]`; a bundle
   with a tampered attestation fails it with `invalid` (never read as merely
   unverified); evaluating `attestation valid` without a key refuses with the
   cannot-verify reason, not a silent downgrade.
5. **A forged dossier cannot ride a valid attestation.** A bundle carrying an
   honest, signed attestation plus a hand-written `evidence/fault-sweep.json`
   claiming `passed 12/12` (bytes not hashing to the signed binding) is
   refused: the binding check grades the attestation `invalid` and the report
   names the hash mismatch. Same outcome for a dossier copied wholesale from
   a simpler component. A dossier present in the bundle but never bound in
   the signed payload gains nothing from the attestation clause: its facet
   stays self-attested and needs the 6.3 acknowledgment.
6. **Attestation is verified against the rebuilt IR.** A component resolved
   with a valid attestation, then modified before admission, is refused: the
   admission-time check rebuilds the IR from the source actually being
   admitted, and the original signature no longer verifies against it.
7. **Unreachable steps block `full`.** A dossier recording
   `passed == steps == 12` with `unreachable == 8` satisfies neither
   `fault-sweep full` nor `fault-sweep 12/12`; the report shows the recorded
   unreachable count against the requirement.
8. **Malformed thresholds die at parse.** `fault-sweep 8/12` (numerator below
   denominator) is a `PolicyError`; so is `requires register keyed` in slice
   1 ("not yet recordable; lands with 309's ledger"); so is any unknown
   facet, status, or register level, and any numeric-confidence spelling
   (`confidence`, a bare float).
9. **Unrooted thresholds are refused unless acknowledged.** `component
   vendored-* requires evidence [fault-sweep full]` without acknowledgment
   is a `PolicyError` at policy load; with the `self-attested` suffix (or a
   policy-level `evidence-root: local`) it loads and evaluates, and the
   report marks the rule self-attested in the body and in `--json`. Before
   the dossier binding lands, adding `attestation valid` does NOT lift the
   error for sweep/gauntlet/inverse thresholds; once the binding is shipped,
   a binding-covering `attestation valid` clause does.
10. **MCP drafts can satisfy the gauntlet clause in slice 1.** An
    MCP-admitted agent draft that survived the session's own gauntlet run
    satisfies `mcp requires evidence [gauntlet admissible]` (the live
    dossier is in the evidence map, no attestation root needed); a draft
    admitted without a gauntlet run is refused with `gauntlet unavailable`.
11. **The gate stays a hard predicate.** Property test: for any bundle and
    any policy, the verdict is a deterministic function of the recorded
    facts and the thresholds; no ordering among components affects any
    verdict.
12. **Nothing widens.** A component refused by a deny/allow reach rule stays
    refused whatever its evidence; approval-required capabilities still
    require the `with` edge for a component with maximal evidence.
13. **Dry-run agrees with the gate.** On a corpus of policies x compositions,
    the set of components `revl policy evaluate` reports "would be REFUSED"
    equals the violations `revl audit --policy` / `enforce` produce.
14. **Register floor honors 309's partial order** (lands with 309's ledger,
    per section 9). A witnessed inverse at the bare `declared` register
    behind `inventory.*` is refused by `capability inventory.* requires
    register keyed`; the same declaration with an idempotency key admits. A
    `keyed` declaration satisfies a `strong` floor. With one `declared` and
    one `keyed` declaration behind the same token, a `keyed` floor refuses
    (worst register wins) and the why-trace names the weakest declaration.
    (The `shape-proven` floor this test also named was removed by item 207.)
15. **Inert selectors are reported.** An evidence rule whose selector matches
    no component in the audit graph (`component Csv*` against a component
    named `csv-reader`) is reported inert in the evaluate report body and in
    `--json`, mirroring item 249's inert-taint precedent.
16. **Byte-identical when absent.** A policy file with no evidence/register
    rules parses to a `Policy` that evaluates byte-identically to today
    (existing policy test suite green, `is_empty` unchanged for empty files).

## 11. Open questions

- Whether `capability <glob> requires evidence` should select on the DECLARED
  capability surface (manifest) or the G8 REACH (audit graph). This design says
  reach, matching every other rule family; revisit if 294's parameterized
  capabilities want the declared-valuation side.
- Where the evidence bundle rides during MCP admission of a RESOLVED
  candidate (the resolve result carries facets today; the load call needs
  the bundle or the entry dir). The session-gauntlet plumbing for drafts is
  decided (section 4); this question is only about resolved candidates.
  Slice 1 will pick the narrowest plumbing that keeps `session.load`
  byte-identical when no evidence rules exist.
- Whether `wouldBeRefused` on resolve candidates should also FILTER under a
  flag (`resolve(policy=...)`), turning prediction into pre-filtering. Deferred:
  filtering at resolve duplicates the gate's job and risks divergence; the
  marker plus the gate is sufficient until proven otherwise.
