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
- a declaration carries the `declared`, `keyed`, `sweep-evidenced`, or
  `shape-proven` register (the item-44/309 honesty ledger).

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
  recorded coverage `(passed, steps)` satisfies `steps >= 12 and passed ==
  steps`: at least that many steps were swept and every swept step passed.
  `fault-sweep full` holds iff `passed == steps > 0`. Both are integer
  comparisons over the recorded counts; a `passed` dossier with zero steps is
  honest but weightless (`partial`, per `_sweep_status`) and satisfies neither.
  The numeric form exists because "full" floats with the program: an operator
  pinning a floor wants "a sweep of at least this size, fully passed".
- `publisher trusted` is graded against the trust set the EVALUATION supplies
  (section 4), mirroring `resolve(trusted_publishers=...)`. A component cannot
  self-assert trust.

### 3.2 Line DSL

Extending the item-33 DSL (docs/boundary-policy.md), same subjects as the reach
rules plus a capability-scoped form:

```
component <glob>  requires evidence [<facet> <threshold>, ...]
realm <name>      requires evidence [<facet> <threshold>, ...]
mcp               requires evidence [<facet> <threshold>, ...]
capability <glob> requires evidence [<facet> <threshold>, ...]
capability <glob> requires register <level>
```

Examples:

```
# only fully-swept, attested components anywhere in this composition
component *        requires evidence [attestation valid, fault-sweep full]

# the agent sandbox: agent-admitted code must have survived the gauntlet
mcp                requires evidence [gauntlet admissible]

# anything reaching payments must come from a trusted publisher, attested
capability payments.* requires evidence [publisher trusted, attestation valid]

# a witnessed inverse behind inventory.* may not be a bare trust-me claim
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

`requires register <level>` is the declaration-strength floor over the
item-44/309 honesty ledger. The ladder, weakest first:

```
declared < keyed < sweep-evidenced < shape-proven
```

mapping onto 309's verification-ledger columns: `declared` is the bare item-44
trust-me register, `keyed` adds the idempotency-key discipline (309),
`sweep-evidenced` means the claim was exercised by the fault sweep
(value-digest checked), `shape-proven` means the static proof holds. The rule
selects components reaching the capability and refuses any whose relevant
declarations (today: the witnessed-inverse and idempotence claims lowered onto
that boundary) sit below the floor. Slice 1 scopes `requires register` to the
registers the IR already records per declaration; it grows as 309's ledger
lands more registers, with the vocabulary staying closed at parse time.

### 3.3 JSON equivalent

Same `Policy`, machine-authored, alongside the existing `components` / `realms`
/ `approvals` keys:

```json
{
  "evidence": [
    {"component": "Csv*",
     "require": {"attestation": "valid", "fault-sweep": "12/12"}},
    {"capability": "payments.*",
     "require": {"publisher": "trusted", "attestation": "valid"}},
    {"mcp": true, "require": {"gauntlet": "admissible"}}
  ],
  "registers": [
    {"capability": "inventory.*", "atLeast": "keyed"}
  ]
}
```

An unknown facet name or status is a `PolicyError` at parse time (a closed
vocabulary, like every other rule family): a typo must not become a rule that
silently requires nothing.

### 3.4 Model changes

`Policy` gains two tuples, defaulting empty so every existing policy file
parses byte-identically and `is_empty()` stays honest:

```python
@dataclass(frozen=True)
class EvidenceRule:
    scope: str                        # "component" | "realm" | "mcp" | "capability"
    selector: str                     # glob / realm name ("" for mcp)
    require: tuple                    # ((facet, threshold), ...) conjunction

@dataclass(frozen=True)
class RegisterRule:
    capability: str                   # glob over capability tokens
    at_least: str                     # "declared" | "keyed" | "sweep-evidenced" | "shape-proven"
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
  definition. Compositions that admit local drafts simply scope their evidence
  rules (`component vendored-*` rather than `component *`), or run producers
  locally (next point).
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
attested + fully swept can now offer the typed diff `component csv-* requires
evidence [attestation valid, fault-sweep full]`, with the same
would-have-covered accounting. 290 ships no distillation logic; it ships the
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
bundle's own root of trust. The design makes that boundary explicit:

- **The root is the attestation, or the operator's own run.** `attestation
  valid` is a cryptographic check with an operator-held key against the
  rebuilt IR (`attest.verify_attestation`, plus `truc reproduce` for
  artifact-level reproduction). `publisher trusted` is membership in an
  operator-supplied set. Everything else in the bundle is, absent those, a
  self-attested claim.
- **`valid` needs a key, and fails closed without one.** `assess_evidence`
  without a key can grade an attestation at most `present` (well-formed, not
  verified). A policy demanding `attestation valid` evaluated without a
  verification key REFUSES with a distinct reason ("cannot verify is not
  valid"), mirroring the shipped `verify_required` resolve which refuses to run
  keyless rather than silently downgrading.
- **A loud warning for unrooted thresholds.** An evidence rule that thresholds
  non-attestation facets while requiring neither `attestation valid` nor
  `publisher trusted` anywhere in its clause list gets an
  `UnrootedEvidenceWarning` (same pattern as item 249's
  `InertTaintPolicyWarning`): "this rule thresholds self-attested evidence;
  add `attestation valid` to root it". A warning, not an error: an operator
  thresholding evidence they produced themselves (private registry, local
  recompute) is legitimate, and the gate must not pretend to a guarantee it
  cannot check, in either direction.
- **The report restates the caveat.** Like the item-33 report restates the G8
  lying-pure-extern caveat, every `revl policy evaluate` report states which
  facets were verified (attestation checked with a key, recomputed locally)
  and which were read from the published bundle as claims.

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
  rule: component * requires evidence [attestation valid, fault-sweep 12/12]
    attestation      valid            (verified with key)          PASS
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

**Slice 1: the rule kind.** `EvidenceRule`/`RegisterRule` in `policy.py` (DSL +
JSON parsing, closed vocabulary, `PolicyError` on unknown facet/status/level),
evaluation over a supplied `{name: EvidenceBundle}` via
`registry.assess_evidence`, `evidence`/`register` violations with why-traces,
`UnrootedEvidenceWarning`, keyless-`valid` fail-closed, composition tests with
the shipped families. `revl audit --policy` gains `--evidence`/`--key`/
`--trusted-publisher` plumbing so the gate can actually see bundles.

**Slice 2: `revl policy evaluate`.** The explain mode on `policy.evaluate`
(every clause verdict, pass and fail), the verb with the CLI shape of section
7 including registry mode, JSON output, the gate-agreement golden, the
resolve-side `wouldBeRefused` marker.

**Slice 3: recomputed evidence + register depth.** `--recompute` running local
producers and marking facets `recomputed` vs `published`; `requires register`
widened as 309's ledger lands its remaining registers; distillation (251)
taught to emit evidence rules stays in 251's own item, unblocked by slice 1.

## 10. Exit tests

1. **High evidence admits.** A component whose bundle grades `attestation
   valid` (real key, real signature) and `fault-sweep 12/12` is admitted under
   `component * requires evidence [attestation valid, fault-sweep full]`; the
   evaluate report shows both clauses PASS with the recorded facts.
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
5. **Unrooted rule warns.** `component * requires evidence [fault-sweep full]`
   alone raises `UnrootedEvidenceWarning`; adding `attestation valid` or
   `publisher trusted` silences it.
6. **The gate stays a hard predicate.** Property test: for any bundle and any
   policy, the verdict is a deterministic function of the recorded facts and
   the thresholds; no ordering among components affects any verdict; the
   grammar rejects any numeric-confidence spelling (`confidence`, a bare
   float) as a `PolicyError`.
7. **Nothing widens.** A component refused by a deny/allow reach rule stays
   refused whatever its evidence; approval-required capabilities still require
   the `with` edge for a component with maximal evidence.
8. **Dry-run agrees with the gate.** On a corpus of policies x compositions,
   the set of components `revl policy evaluate` reports "would be REFUSED"
   equals the violations `revl audit --policy` / `enforce` produce.
9. **Register floor.** A witnessed inverse at the bare `declared` register
   behind `inventory.*` is refused by `capability inventory.* requires
   register keyed`; the same declaration with an idempotency key admits.
10. **Byte-identical when absent.** A policy file with no evidence/register
    rules parses to a `Policy` that evaluates byte-identically to today
    (existing policy test suite green, `is_empty` unchanged for empty files).

## 11. Open questions

- Whether `capability <glob> requires evidence` should select on the DECLARED
  capability surface (manifest) or the G8 REACH (audit graph). This design says
  reach, matching every other rule family; revisit if 294's parameterized
  capabilities want the declared-valuation side.
- Where the evidence bundle rides during MCP admission of a resolved candidate
  (the resolve result carries facets today; the load call needs the bundle or
  the entry dir). Slice 1 will pick the narrowest plumbing that keeps
  `session.load` byte-identical when no evidence rules exist.
- Whether `wouldBeRefused` on resolve candidates should also FILTER under a
  flag (`resolve(policy=...)`), turning prediction into pre-filtering. Deferred:
  filtering at resolve duplicates the gate's job and risks divergence; the
  marker plus the gate is sufficient until proven otherwise.
