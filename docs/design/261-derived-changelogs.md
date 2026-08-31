# Derived changelogs - the release note is computed, not written (item 261)

**Status: design, not implemented.** This document specifies `revl changelog`,
a renderer that computes a human-readable release note from the STRUCTURAL delta
between two generations of a composition. Every line is a projection of a fact
the drift machinery already produces: `composition_diff.diff` (item 123, the
IR-level structural delta), `version.derive` (item 64, the computed semver bump),
and the `audit_diff` authority surface (item 64's foundation). It invents no
prose. A line with no backing fact is a defect, not a feature, and the design is
built so the renderer physically cannot emit one.

The distinction the feature draws: a hand-written release note is a PROMISE
about what changed, trusted because a human typed it. A derived changelog is a
MEASUREMENT of what changed, read off two IRs. The one thing an operator most
needs the note to be right about - what the new version may now reach outside
the system - is exactly the thing a hand-written note is most often wrong about,
because the author did not notice the new emission. The computer noticed.

## Revision (adversarial review 2026-08-31)

A second adversarial pass found that the v1 completeness guard, and the v1
headline, were both weaker than v1 claimed. Four defects, folded in below. The
short version: the guard operated at TOP-LEVEL-KEY granularity while the differs
consume SUB-FIELDS of those keys, so a nested widening under a "covered" key
escaped both the differ and the guard; and an unclassified fact did not floor
the headline, so a body carrying a breaking-ish honesty line could still
headline PATCH.

**1. CRITICAL (new). The completeness guard was blind to nested sub-fields of a
"covered" top-level key.** v1's guard asserted every TOP-LEVEL key of
`audit_report(after)` is either consumed by a differ or in
`UNDIFFED_AUDIT_SURFACES`. `boundary` counts as "covered" because
`diff_crossings` reads it, so the guard never descended into it. But
`diff_crossings` reads only `boundary[c].emissions`, `externs[].name`, and
`taint`. The per-emission capability scope map `boundary[c].capabilities[label]`
(a NESTED field of the "covered" `boundary` key, built at `__main__._boundary`
~134/234) is differenced by NOTHING: not by `diff_crossings` (the `emit:C:label`
token is scope-free, so a `send.mail -> send.*` widening on a STABLE crossing
leaves the token unchanged), not by `version.derive` (an emission scoped at the
component boundary is not a service method, so `_service_compatible` never sees
it), not by `composition_diff._emission_scope` (called only inside the
`crossings.added` loop ~232 as a display tail, never as a difference), and not by
the guard (its parent key is "covered"). The v1 §6 HIGH claimed this was
surfaced as an unclassified honesty line. It was WRONG: v1 dropped it silently.
Second instance of the same flaw: `externs` is "covered" by `diff_reach`, which
reads only `externs[].reach`; an extern that GAINS A NEW BACKEND host body
(`backends: ["rust"] -> ["py","rust"]`, i.e. new reachable host code, fields at
`audit_diff.py` ~44-57) is likewise dropped by both differ and guard. (The v1
dropped-undo case survived ONLY because a class/undo change also mutates the
top-level `recovery_surface`, which IS in the undiffed set. That was luck, not
coverage.)

Fix: the guard is redefined at the granularity the differs actually CONSUME, not
the top-level key. We take approach (b): the guard is a STRUCTURAL DIFF of the
WHOLE `audit_report(before)` against `audit_report(after)`, from which we
subtract EXACTLY the leaf paths a differ demonstrably reads (the
`CONSUMED_PATHS` allowlist: `boundary[*].emissions`, `boundary[*].externs[*].name`,
`boundary[*].taint`, `externs[*].reach`, plus the crossing/secret projections).
Any residual path that shows a delta and is not in `CONSUMED_PATHS` emits an
unclassified honesty line. Coverage is now a property of the leaf, so a nested
widening under a "covered" key can no longer hide: `boundary[*].capabilities` and
the un-read `externs[*].{backends,class,register,idempotent}` are outside
`CONSUMED_PATHS`, so their deltas honesty-line in Slice 1 and become first-class
differs in Slice 2 (§4, §7). The Slice-1 exit suite gains a
`boundary[c].capabilities` scope-widening on a STABLE crossing asserting an
honesty line, and the new-backend-host-body case. The v1
dropped-recovery-inverse test alone would have passed this hole wide open.

**2. HIGH. An unclassified fact did not floor the headline.** v1 computed
`headline = max(version.bump, authority_bump, wiring_bump)`. An unclassified fact
(a dropped recovery inverse surfaced via `recovery_surface`) feeds NONE of the
three terms, so the headline computed PATCH while the honesty line sat in a body
section the operator may never open. v1's "never headline PATCH over a breaking
line" rule was technically satisfied (the line is unclassified, not breaking) and
that is precisely the loophole; the headline also escaped the fact-bijection test
because it is a `headline` object, not a `ChangelogLine`. Fix (§3, §4): a
NON-EMPTY unclassified bucket forces the headline non-clean. The headline may
claim a DEFINITE bump level ONLY when the unclassified bucket is empty; otherwise
it refuses a clean level and carries an incompleteness marker, e.g.
`PATCH? (unclassified authority/recovery changes present; review body)`. A dropped
recovery inverse now makes the headline non-clean, never a clean PATCH.

**3. MEDIUM. The guard keyed on `after` alone, missing a removed optional
surface.** `parallel_plan` (and `secrets`) is conditionally present; if a
generation removes its last parallel group the key is in `before` not `after`,
and a guard iterating `after.keys()` never enumerates it. Fix (§4): the guard is
driven over `set(audit_report(before)) | set(audit_report(after))` (and, under
approach (b), the union of leaf paths on both sides). Exit test: removing an
optional surface trips the guard.

**4. LOW. Raw-delta over unhashable audit structures was underspecified.**
`recovery_surface` is `list[dict]` and `capability_registers` is `dict`; a naive
`set(before) ^ set(after)` throws on unhashable dicts, and a `len()` delta hides
same-count reshuffles. Fix (§4): the raw-delta comparison canonicalizes each
undiffed surface to sorted-key JSON and compares the serializations; the honesty
line reports a TYPED/BOOLEAN delta (changed-yes/no plus which paths moved), never
a bare count, so a same-length reshape is reported as a change, not as no-change.

**PO note (item 64 ownership).** This design correctly surfaces that item 64's
`audit_diff` foundation is narrower than the roadmap text implies: only
`diff_crossings` (crossings) and `diff_reach` (reach) are differenced today.
Slice 2's `diff_capability_scopes` / `diff_backends` / `diff_recovery` /
`diff_registers` / `diff_cardinality` helpers HARDEN item 64's `audit_diff`
foundation and belong THERE (in `audit_diff.py`, reused by the `evaluate` gate),
not only in 261. They are recorded here as an item-64 debt this feature both
exposes and pays down.

The sections below are revised in place to match. Where v1 §6 asserted the
capability-scope HIGH was "surfaced as unclassified," that claim is corrected:
under the v1 guard it was silently dropped; under the revised path-granular guard
it IS surfaced.

## 0. What already exists (the seams this lands on)

Grounding, so the design reuses real machinery and reinvents nothing.

- **The structural delta.** `composition_diff.diff(before, after)`
  (`src/revl/composition_diff.py`, 306 lines) returns a dict with
  `components.{added,removed,changed}`, `providers.{added,removed,changed}`,
  `requires.{added,removed,broken}`, `crossings.{added,removed}`, and a
  pre-rendered `guarantees` list of sentences (`component X gained emission
  cache.put`, `provider of key db changed from PgDatabase to MysqlDatabase`).
  It already suppresses per-crossing noise for whole added/removed components,
  and it already carries the emission SCOPE tail (`[bus]`) via `_emission_scope`.
- **The computed semver bump.** `version.derive(previous, current_ir,
  previous_version)` (`src/revl/version.py`) returns `{bump, changes[],
  previousVersion, nextVersion}`, where each `change` is
  `{service, method, kind, bump, reason}` classified by the REAL admission
  predicate `_service_compatible` in the consumer regime. This is the
  authoritative BREAKING-vs-additive verdict for the interface axis, and it is
  reused verbatim, never re-derived.
- **The authority surface.** `audit_diff.diff_crossings(prev, new)` returns
  sorted `added` / `removed` crossing tokens (`emit:Comp:label`,
  `host:Comp:name`, `taint:...`, `declassify:...`) plus `reach_weakened` /
  `reach_tightened` (item 373). `audit_diff.audit_report(ir)` also carries
  surfaces no differ yet consumes: `capability_registers`, `recovery_surface`,
  `cardinality`, `distributability`, `parallel_plan`, and the per-component
  `boundary[comp].capabilities` scope map. Section 6 shows why that gap is the
  CRITICAL finding.
- **The two-input loader.** `composition_diff.load_composition(path)` accepts a
  `.rvl` source (compiled on the spot), a compiled IR document
  (`revl compile -o`), or an `revl audit --json` interchange doc. `revl diff`
  and `revl profile` both route through it, so detection never drifts. The
  changelog reuses it unchanged.
- **The verb wiring.** `cli/parser.py` builds each subcommand; `__main__.main`
  dispatches on `args.command`; `revl diff` routes to `cli/observe._run_diff`
  BEFORE the shared single-source compile step, because it has two inputs.
  `revl changelog` slots in exactly there, next to `diff`.

## 1. The surface

**A new verb, `revl changelog`, not a flag on `revl diff`.**

```
revl changelog --from OLD --to NEW [--json] [--no-semver] [--title TEXT]
```

`OLD` and `NEW` each accept the same three forms `revl diff` accepts (a `.rvl`
source, a compiled IR doc, or an `audit --json` doc), loaded through the shared
`load_composition`. Default output is Markdown (a release note you paste under a
version heading). `--json` emits the structured changelog document (section 4)
for a registry or a bot to consume.

Why a distinct verb rather than `revl diff --changelog`:

- **Different audience, different projection.** `revl diff` is the PR-review
  tool: it speaks in guarantees to an agent reviewing one change, positional
  `BEFORE AFTER`, structural only. `revl changelog` is the release tool: it
  speaks to an operator deciding whether to deploy, it LEADS with authority and
  semver impact, and it folds in the item-64 bump that `diff` does not compute.
  Overloading `diff` with a second output shape and a semver dependency would
  blur a clean tool.
- **The roadmap states this surface.** Item 261: `revl changelog --from a.json
  --to b.json`. The named flags `--from`/`--to` (not positional) read correctly
  for a release note and match the item text.
- **The registry attaches it (item 49).** A published version carries its
  changelog; a stable verb name is the natural attach point, and the item-64
  bump it headlines is the same one the registry refuses-on-contradiction.

The verb is pure reuse. It computes NO delta of its own that an existing helper
already computes (section 5). Its module, `src/revl/changelog.py`, is a
classifier-and-renderer over `diff`, `derive`, and the `audit_diff` helpers.

## 2. The fact-to-line mapping (no line without a backing fact)

The renderer is a fold over a single ordered list of `ChangelogLine` records.
Each record is:

```
ChangelogLine(
    fact:     str,   # provenance token: the exact upstream fact this derives from
    category: str,   # breaking | added | internal | unclassified
    lede:     bool,  # authority-relevant -> sorts to the top of its category
    text:     str,   # the rendered sentence
)
```

`fact` is mandatory and non-empty by construction: a line is only ever built
inside a loop over one upstream fact, and the constructor asserts `fact`. There
is no code path that appends free text. This is the structural guarantee behind
"every line traces to a fact" (the exit test in section 4 checks the inverse:
every emitted line's `fact` resolves back to an input fact).

The full enumeration. Each row is one fact SOURCE, its rendered form, and its
default classification (section 3 justifies the classification):

| Source fact (upstream)                                  | `fact` token                     | Rendered line                                                         | Class      |
|---------------------------------------------------------|----------------------------------|-----------------------------------------------------------------------|------------|
| `diff.crossings.added` = `emit:C:label` (scope `[s]`)   | `crossing.added:emit:C:label`    | `C gained emission label [s]` (widening)                              | breaking   |
| `diff.crossings.added` = `host:C:name`                  | `crossing.added:host:C:name`     | `C now reaches host code name`                                        | breaking   |
| `diff.crossings.added` = `taint:C:origin`               | `crossing.added:taint:C:origin`  | `C now routes untrusted origin into an emission`                      | breaking   |
| `diff.crossings.added` = `declassify:C:origin`          | `crossing.added:declassify:...`  | `C now declassifies untrusted origin`                                 | breaking   |
| `audit_diff.reach_weakened:name`                        | `reach.weakened:name`            | `extern name loosened its reach bound`                                | breaking   |
| `diff.crossings.removed` = `emit:C:label`               | `crossing.removed:emit:C:label`  | `C no longer emits label`                                             | added*     |
| `audit_diff.reach_tightened:name`                       | `reach.tightened:name`           | `extern name tightened its reach bound`                               | added*     |
| `version.change` bump=major                             | `semver:S.m:kind`                | the change's own `reason` (e.g. `send narrowed parameter 1`)          | breaking   |
| `version.change` bump=minor                             | `semver:S.m:kind`                | the change's own `reason` (e.g. `poll is new`)                        | added      |
| `diff.components.added` = C                              | `component.added:C`              | `component C added`                                                   | added      |
| `diff.components.removed` = C                            | `component.removed:C`            | `component C removed`                                                 | breaking   |
| `diff.providers.changed` (interface moved)              | `provider.changed:key`          | `provider of key changed from A to B (service S1 -> S2)`              | breaking   |
| `diff.providers.changed` (same service, comp swapped)   | `provider.swapped:key`          | `provider of key swapped from A to B (same service S)`                | internal** |
| `diff.providers.added` = key                            | `provider.added:key`            | `key key is now provided by S`                                        | added      |
| `diff.providers.removed` = key                          | `provider.removed:key`          | `key key is no longer provided`                                       | breaking   |
| `diff.requires.added` = (C,key)                         | `require.added:C:key`           | `C now requires key`                                                  | added      |
| `diff.requires.removed` = (C,key)                       | `require.removed:C:key`         | `C no longer requires key`                                            | internal   |
| `diff.requires.broken` = (C,key)                        | `require.broken:C:key`          | `C requires key - no provider (broken dependency)`                    | breaking   |

`*` a removed crossing is strictly-purer (safe direction, section 3); it renders
under Added/relaxed, not Breaking. `**` a same-service provider swap is
behaviorally live but interface-compatible - see the CRITICAL-adjacent case in
section 6, Attack A: it is never folded into "no change," it always renders.

Audit-surface deltas beyond crossings and reach (registers, recovery, cardinality,
capability scope) are NOT in this table for Slice 1 and are handled by section 6's
honesty line until Slice 2 lands their differs. That omission is the CRITICAL.

Most of these rendered strings already exist as `composition_diff._guarantees`
output. The changelog does not re-author them; it CONSUMES `diff.guarantees`
where possible and only adds the classification and grouping. The table's `fact`
tokens are keyed to the same structural facts `_guarantees` iterates, so the two
stay in lockstep (section 5).

## 3. Classification - deriving semver-relevant impact from the structural delta

Three buckets, in the order an operator reads them.

**Breaking (the lede).** A change that invalidates a running consumer OR widens
what the composition reaches outside the system. Authority widenings sort FIRST
within this bucket (`lede=True`), because item 261's rule is "authority changes
are the lede, not the footnote." The breaking set is the union of:

- every `version.change` with `bump == "major"` (removed method/service,
  narrowed parameter, widened return, arity / `async` / `commutative` flip,
  introduced emission, WIDENED capability scope on a service method) - the
  authoritative interface verdict, reused;
- every ADDED crossing (`emit` / `host` / `taint` / `declassify`) - a new
  boundary reach is a widening, the same direction `audit_diff.evaluate` fails;
- every `reach_weakened` - a bound loosened on a crossing that stayed;
- `components.removed`, `providers.removed`, `providers.changed` (service
  interface moved), and `requires.broken` - the wiring axis breaks.

**Added / relaxed (compatible).** New surface that breaks no running consumer,
and authority GIVEN UP (the safe direction):

- every `version.change` with `bump == "minor"` (added method/service, widened
  parameter, narrowed return, narrowed capability scope, `emission-loss`);
- `components.added`, `providers.added`, `requires.added`;
- REMOVED crossings and `reach_tightened` - strictly purer.

**Internal.** Live but externally-invisible: a same-service provider swap, a
removed require edge that leaves a still-satisfied graph. Rendered under a
collapsed "Internal / wiring" heading so it is present but not shouted.

**The headline** is the item-64 bump: `version.derive(...).bump`, rendered as
`MAJOR` / `MINOR` / `PATCH`, with `nextVersion` when `--current-version` (or the
registry) supplies the previous one. The join rule is version.py's: any major ->
major, else any minor -> minor, else patch. Crucially the headline bump is read
from `version.derive` ALONE (the interface axis), but the changelog BODY lists
the authority and wiring changes too, and section 6 Attack A forces the headline
to be corrected UP when an authority widening or a live wiring change outranks
what the interface diff saw. The rule: `headline = max(version.bump,
authority_bump, wiring_bump)`, where an added crossing or weakened reach floors
the authority bump at major, and a broken dependency floors the wiring bump at
major. A changelog must never headline PATCH over a body that contains a
breaking line.

**Conservative under unclassified (revision finding 2).** `max(version,
authority, wiring)` is not enough, because an UNCLASSIFIED fact feeds none of the
three terms yet may be the most breaking thing in the release (a dropped recovery
inverse reaches the body only via `recovery_surface`, an undiffed surface). The
invariant: **the headline may claim a DEFINITE bump level only when the
unclassified bucket is empty.** When the unclassified bucket is NON-EMPTY the
headline refuses a clean level and carries an incompleteness marker rather than a
bare `MAJOR`/`MINOR`/`PATCH`:

```
PATCH? (unclassified authority/recovery changes present; review body)
```

The `?` and the parenthetical are structural, not cosmetic: a non-empty
unclassified bucket sets `headline.clean = false` and the renderer refuses to
print a clean level. This closes the loophole where the "never headline PATCH
over a breaking line" rule was technically satisfied because the honesty line was
unclassified rather than classified-breaking. Equivalent framing: §4 treats an
unclassified fact as potentially-breaking, so it MUST be able to move the
headline; since it carries no derivable level, it moves it to non-clean rather
than to a specific bump the renderer cannot honestly justify.

## 4. Determinism and honesty

**Pure function of the two IRs.** `derive_changelog(before, after)` reads only
the two IR documents. No wall-clock, no environment, no filesystem beyond the
two inputs. The Markdown body contains NO timestamp: a release date, if wanted,
is passed via `--title`/metadata by the operator and lives in a header the
renderer treats as opaque, never in a derived line. `--json` carries a
`generatedFrom: {fromLabel, toLabel}` echo of the input labels and nothing
time-varying.

**Stable ordering.** Every upstream source is already sorted: `composition_diff`
sorts all its lists, `version.diff_services` sorts service-then-method,
`audit_diff.diff_crossings` sorts each bucket. The changelog's only ordering
choice is (a) category order (fixed enum: breaking, added, internal,
unclassified), and (b) `lede` first within Breaking (a stable partition that
preserves the upstream sorted order inside each half). No set is iterated
without a sort. Exit test: `derive_changelog(a,b)` rendered twice is
byte-identical, and rendering is invariant under re-serializing either input
dict (key order in the JSON must not matter).

**Honesty - the unclassified bucket.** A fact the classifier has no rule for is
NEVER dropped. It renders under a final `### Unclassified changes (review
manually)` heading, one line per fact, carrying the raw `fact` token. The
default for an unrecognized category is unclassified-and-visible, NOT
silently-additive - an unknown change is treated as potentially breaking for the
operator's attention, never hidden. Two concrete sources feed it:

- a `version.change.kind` the changelog's kind->text map does not recognize
  (a future drift kind added to `version.py`) - rendered from its own `reason`;
- any audit-surface delta the changelog does not yet difference (Slice 1: all
  of registers / recovery / cardinality / capability-scope) - see section 6.

**The completeness guard (revised: path-granular, whole-document).** The v1
guard asserted every TOP-LEVEL key of `audit_report(after)` was differenced or
in an `UNDIFFED_AUDIT_SURFACES` set. That was too coarse: a differ that "covers"
a key reads only some LEAF PATHS under it, so a nested widening under a "covered"
key escaped both the differ and the guard (revision finding 1). The guard is
redefined at the granularity the differs actually consume.

Guard construction (approach (b), whole-document structural diff):

- Compute `audit_report(before)` and `audit_report(after)` and take their
  STRUCTURAL diff: the set of leaf paths whose value differs, added, or removed.
  A leaf path is a dotted/indexed access like `boundary.Front.capabilities.notify`
  or `externs[3].backends`.
- Drive enumeration over `set(before-paths) | set(after-paths)`, NOT `after`
  alone (revision finding 3), so a REMOVED optional surface (`parallel_plan`,
  `secrets`) present only in `before` is still seen.
- Subtract `CONSUMED_PATHS`: the explicit allowlist of leaf paths a declared
  differ demonstrably READS. For Slice 1 that is exactly
  `boundary[*].emissions`, `boundary[*].externs[*].name`, `boundary[*].taint.*`
  (all consumed by `diff_crossings` via `crossings()`), and `externs[*].reach`
  (consumed by `diff_reach`), plus the top-level `secrets` projection those
  differs read. Every path in `CONSUMED_PATHS` is a leaf a real differ reads, and
  the guard test asserts that correspondence so the allowlist cannot rot.
- Any residual differing path (in the structural diff, not in `CONSUMED_PATHS`)
  emits one UNCLASSIFIED honesty line, keyed by its path. This is where
  `boundary[*].capabilities.*`, `externs[*].{backends,class,register,idempotent}`,
  `recovery_surface`, `capability_registers`, `cardinality`, `distributability`,
  and `parallel_plan` land in Slice 1: their parent key may have a differ, but the
  LEAF is not in `CONSUMED_PATHS`, so a delta on it is surfaced, never dropped.

Coverage is thus a property of the LEAF a differ reads, not of a top-level key.
A newly-added audit surface, OR a new sub-field of an existing surface, therefore
either gets added to `CONSUMED_PATHS` (which the test forces to correspond to a
real differ) or honesty-lines. It cannot silently escape the changelog. This is
the mechanical enforcement of "no verifiable change is dropped," now at the
granularity where v1 leaked.

**Raw-delta over unhashable surfaces (revision finding 4).** The structural diff
canonicalizes each surface value to sorted-key JSON (`recovery_surface` is
`list[dict]`, `capability_registers` is `dict`) and compares the
serializations; it never does `set(before) ^ set(after)` over unhashable dicts.
The honesty line reports a TYPED/BOOLEAN delta (changed yes/no, plus which leaf
paths moved), never a bare `len()` count, so a same-length reshape (two recovery
entries reordered or swapped register) is reported as a change, not as
no-change.

The `--json` document shape:

The `headline` carries `clean`: `true` only when the unclassified bucket is
empty (the headline may then claim a definite level), `false` otherwise (the
renderer prints the `PATCH? (...)` incompleteness marker instead of a clean
level). An unclassified honesty line carries `changed: true` and the `paths` that
moved, never a bare count. A clean release:

```json
{
  "headline": {"clean": true, "bump": "major", "previousVersion": "1.4.2", "nextVersion": "2.0.0"},
  "breaking": [{"fact": "crossing.added:emit:Front:cache.put", "text": "Front gained emission cache.put [bus]", "lede": true}],
  "added":    [{"fact": "component.added:Metrics", "text": "component Metrics added"}],
  "internal": [{"fact": "provider.swapped:db", "text": "provider of key db swapped from PgDatabase to MysqlDatabase (same service Database)"}],
  "unclassified": [],
  "generatedFrom": {"fromLabel": "v1.json", "toLabel": "v2.json"}
}
```

A release whose only detected change is undiffed (Slice 1, dropped recovery
inverse) headlines NON-clean, even though `version`/authority/wiring all read
PATCH:

```json
{
  "headline": {"clean": false, "marker": "PATCH? (unclassified authority/recovery changes present; review body)", "bump": "patch"},
  "breaking": [],
  "added": [],
  "internal": [],
  "unclassified": [{"fact": "audit-path:recovery_surface", "text": "recovery surface changed - not yet differenced; review manually", "changed": true, "paths": ["recovery_surface[1].kind"]}],
  "generatedFrom": {"fromLabel": "v1.json", "toLabel": "v2.json"}
}
```

## 5. Interaction with `revl diff`, `revl version`, and the audit gate

**Reuse, never duplicate.** The delta computation lives in three existing
modules; the changelog imports their results:

- `composition_diff.diff(before, after)` -> the whole membership/wiring/crossing
  structural delta AND its pre-rendered `guarantees` sentences. The changelog
  consumes `diff.guarantees` for the rendered TEXT of the structural lines,
  adding only classification and the `fact` token. This means a wording fix in
  `_guarantees` propagates to the changelog with zero changelog edits, and the
  two can never disagree about what a structural change SAYS.
- `version.derive(previous, current_ir, prev_version)` -> the semver headline
  and the per-operation major/minor classification. The changelog does not
  re-run `_service_compatible`; it reads `derive`'s `changes[]`.
- `audit_diff.diff_crossings` / `diff_reach` -> already invoked transitively by
  `composition_diff.diff` (it stores `crossings.{added,removed}`). The changelog
  reads them off the `diff` result rather than calling `audit_diff` a second
  time, so the crossing set is computed exactly once.

**The one divergence from `diff`'s inputs.** `version.derive` needs the
`services` interface table, which a compiled IR doc carries but an `audit --json`
interchange doc does not (version.py raises a pointed error). The changelog does
NOT raise: given a bare audit doc it DEGRADES honestly - it emits the full
structural + authority changelog and headlines `bump: undetermined (inputs lack
the interface table; run against compiled composition docs for a semver
headline)`. Structural honesty survives a degraded input; only the semver
headline is withheld, and its absence is stated, not faked. `--no-semver` forces
this mode explicitly.

**The audit gate stays separate.** `revl audit --diff` (the `evaluate` gate) is
the PASS/FAIL authority wall with exit codes and `--accept` tokens. `revl
changelog` is a RENDER, always exit 0, no acknowledgement model. The changelog
REPORTS a widening as breaking; the gate REFUSES it. They share
`diff_crossings`; they do not share a decision. A release flow runs the gate to
decide and the changelog to describe.

## 6. Adversarial self-review

Every prior design review here found a CRITICAL. Here is this one's, found first.

### CRITICAL - a dropped recovery inverse renders as "no change"

**Attack.** A `witnessed`/`acquire` extern's `undo` is deleted between versions,
turning a reversible effect into an irreversible one - item 273's exact "class
change," the most security-relevant delta a release note can carry. This shows
up in `audit_report.recovery_surface` (the entry loses its `inverse` kind), and
NOWHERE else: it does not add or remove a crossing (the `host:` reach is
unchanged), it does not touch the `services` interface table (an extern is not a
service method), and it does not move a provider or require edge. Slice 1's
changelog consumes ONLY `diff_crossings` + `diff_reach` + `version` + structural
`diff`. None of them sees `recovery_surface`. The change is SILENTLY DROPPED, and
the changelog headlines PATCH over a composition that just lost its ability to
undo a production effect. The same hole swallows a weakened idempotency
`register` and a raised `cardinality` ceiling - three real widenings, all
invisible.

**Root cause.** The task brief and item 64 describe the audit_diff foundation as
covering "capability/register/recovery/cardinality/secrets deltas," but the
SHIPPED `audit_diff.py` only differences CROSSINGS and REACH. `audit_report`
CARRIES `recovery_surface`, `capability_registers`, `cardinality`,
`distributability` - but no `diff_*` helper consumes them. A changelog built
naively on "the facts revl diff surfaces" inherits exactly that blind spot.

**Mitigation (in the design, not deferred).** Two mechanisms, both in section 4:
(1) the REVISED path-granular COMPLETENESS GUARD - `derive_changelog`
structural-diffs `audit_report(before)` vs `audit_report(after)` over the union
of leaf paths, subtracts `CONSUMED_PATHS` (the leaves a differ reads), and emits
one UNCLASSIFIED honesty line per residual differing path ("recovery surface
changed - not yet differenced; review manually"), which per finding 2 also forces
the headline non-clean. So Slice 1 is honest-but-incomplete: it never claims
silence it cannot back, and it points the operator at the surface. The v1
top-level-key form of this guard was itself defective (it never descended into a
"covered" key); see the Revision section. (2) Slice 2 adds `diff_recovery`,
`diff_registers`, `diff_cardinality`, `diff_capability_scopes`, `diff_backends`
to `audit_diff.py` (their correct home, hardening item 64 and reused by the gate
too), moving each from unclassified to breaking. A DROPPED recovery inverse and a
WEAKENED register are then first-class breaking lines with `lede=True`.
**Status: surfaced honestly by the revised guard in Slice 1, fully closed in
Slice 2. The guard is a Slice-1 exit gate, not optional.**

### HIGH - a capability scope widening on a stable emission label (send.mail -> send.*)

**Attack.** An emission `notify` widens its declared capability scope from
`send.mail` to `send.*`. The crossing token is `emit:C:notify` in BOTH
generations - scope is NOT in the token (`audit_diff.crossings` builds
`emit:{component}:{label}`, scope-free) - so `diff_crossings.added` is empty. If
the scope is declared at the service method, `version.derive` catches it (a
widened capability scope is a `_service_compatible` drift -> major); but a scope
declared at the COMPONENT boundary, on a bare emission with no service-method
capability annotation, is read by `_emission_scope` from
`boundary[C].capabilities[label]` and is invisible to both differs. A genuine
authority widening renders as nothing.

**Correction (revision finding 1).** v1 stated this delta "falls under the
completeness guard's honesty line" in Slice 1. Under the v1 TOP-LEVEL-KEY guard
that was FALSE: `boundary[C].capabilities` is a nested leaf of the `boundary`
key, which `diff_crossings` "covers," so the v1 guard never descended and the
widening was SILENTLY DROPPED (`emit:C:notify` is unchanged, scope is not in the
token). This is the new CRITICAL; see the Revision section. It is only surfaced
once the guard is redefined at leaf-path granularity: `boundary[*].capabilities.*`
is outside `CONSUMED_PATHS`, so its delta honesty-lines in Slice 1.

**Mitigation.** Slice 2's `diff_capability_scopes` differences
`boundary[C].capabilities` label->scope maps between the two audits: a scope that
grows (superset, or `[x]` -> `[*]`) is a BREAKING widening line with `lede=True`;
a scope that shrinks is added/relaxed. Until Slice 2, this delta is surfaced by
the REVISED path-granular completeness guard as an unclassified honesty line
(which, being non-empty, also forces the headline non-clean per finding 2).
**Status: surfaced-as-unclassified in Slice 1 UNDER THE REVISED GUARD (was
silently dropped under the v1 guard), classified breaking in Slice 2.**

### CRITICAL-adjacent - a live provider swap headlined as PATCH

**Attack.** `provider of key db changed from PgDatabase to MysqlDatabase`, both
satisfying the same `Database` service interface and emitting the same labels.
`version.derive` sees no interface change (the service table is identical) ->
PATCH. `diff_crossings` sees identical crossings. A changelog that derives its
headline from `version` alone, and that took a "no interface change -> nothing to
report" fast path, would headline PATCH and omit the swap - silently changing the
production database in a release noted as a bug-fix patch.

**Mitigation.** Two rules from sections 2-3: (1) a same-service provider swap is
NEVER folded into "no change" - it always renders, under Internal/wiring, with
its own `provider.swapped` fact. (2) The "no structural change" fast path is
gated on `diff.changed AND not version.changes AND no audit delta`, never on
`version` alone; a provider swap sets `diff.changed`, so the fast path does not
fire. The operator sees the swap even though semver correctly stays PATCH (the
interface truly did not break). **Status: mitigated.** The residual judgment -
whether a same-service swap should FORCE a minor - is left to the operator by
design: revl cannot know if MySQL and Postgres are behaviorally interchangeable,
so it reports the fact and does not invent a bump it cannot derive.

### MEDIUM - invented prose / a line with no backing fact

**Attack.** A future edit adds a "helpful" summary sentence ("This release
improves reliability") with no fact behind it, eroding the core guarantee.

**Mitigation.** The `ChangelogLine` constructor requires a non-empty `fact`, and
there is NO renderer branch that appends a string outside a fact loop. The
exit-test `test_every_line_has_a_backing_fact` asserts a bijection: every line in
the rendered output carries a `fact`, and every `fact` resolves to a member of
the union of `diff`'s facts, `derive`'s changes, and the audit deltas. A summary
sentence has no resolvable `fact` and fails the test. **Status: mitigated,
structurally + by exit test.**

### MEDIUM - non-deterministic ordering from dict iteration

**Attack.** `audit_report.boundary` and `.externs` are built by dict iteration;
a Slice-2 audit-surface differ that iterated them without sorting would produce
order-dependent changelog lines, making the "pure function" claim false and
release notes diff-noisy.

**Mitigation.** Section 4's rule - no set or map is iterated without a sort - is
a design invariant every differ must honor, and the determinism exit test
(render twice + render under re-serialized inputs, assert byte-identical)
catches a violation. **Status: mitigated by invariant + test.**

## 7. Implementation plan (sliced; first slice lands alone)

**Slice 1 - the honest renderer over existing differs (landable alone).**

- `src/revl/changelog.py`:
  - `ChangelogLine` dataclass (frozen; `fact` asserted non-empty).
  - `classify(diff_result, version_result) -> list[ChangelogLine]` - the
    section-2 table as explicit per-source loops; consumes `diff.guarantees`
    for structural text, `version.changes` for semver text.
  - `_completeness_guard(before_ir, after_ir)` - the REVISED path-granular guard
    (§4): structural-diff `audit_report(before)` vs `audit_report(after)` over
    the UNION of both sides' leaf paths, subtract `CONSUMED_PATHS` (the leaves a
    differ demonstrably reads), and emit one unclassified honesty line per
    residual differing path. Canonicalizes surface values to sorted-key JSON;
    reports a typed/boolean delta with the moved paths, never a bare count. A
    companion test asserts every `CONSUMED_PATHS` entry corresponds to a real
    differ read.
  - `derive_changelog(before, after, previous_version=None, no_semver=False) ->
    dict` - runs `composition_diff.diff` + (`version.derive` unless degraded) +
    guard; returns the section-4 JSON shape; computes
    `headline = max(version, authority, wiring)` AND sets `headline.clean = false`
    with the `PATCH? (...)` marker whenever the unclassified bucket is non-empty
    (§3 invariant).
  - `render_markdown(doc, title=None) -> str`.
- `src/revl/cli/parser.py` - add the `changelog` subparser (`--from`, `--to`,
  `--json`, `--no-semver`, `--current-version`, `--title`), next to `diff`.
- `src/revl/cli/observe.py` - add `_run_changelog(args)`, mirroring `_run_diff`
  (two-input load via `load_composition`); export it.
- `src/revl/__main__.py` - route `args.command == "changelog"` before the shared
  compile step, next to `diff`.
- `docs/revl-changelog.md` - operator doc, one worked example per category.
- Tests: fact-bijection (every line has a resolvable fact), determinism
  (byte-identical under re-render and re-serialized inputs), and the completeness
  guard's regression suite, which MUST include all four of:
  - **CAPABILITY-SCOPE on a STABLE crossing** (revision finding 1): a
    `boundary[c].capabilities[label]` widening (`send.mail -> send.*`) with the
    `emit:c:label` crossing token UNCHANGED produces an honesty line. The v1
    dropped-recovery-inverse test alone passed this hole wide open, so this case
    is mandatory, not optional.
  - **NEW-BACKEND-HOST-BODY** (revision finding 1): an extern with
    `backends: ["rust"] -> ["py","rust"]` (new reachable host code) produces an
    honesty line, since `diff_reach` reads only `reach`.
  - **REMOVED optional surface** (revision finding 3): removing the last
    parallel group (so `parallel_plan` is in `before`, not `after`) trips the
    guard, proving it drives over `before | after`.
  - **RESHUFFLE not hidden** (revision finding 4): a same-length reorder/register
    swap inside `recovery_surface` reports `changed: true`, not no-change.
  - the original dropped-recovery-inverse case (honesty line, not silence).
  - **HEADLINE non-clean under unclassified** (revision finding 2): a dropped
    recovery inverse makes `headline.clean == false` with the `PATCH? (...)`
    marker, NOT a clean PATCH; plus headline-never-understates (a body breaking
    line forces a >= major headline).
  - degraded-input (bare audit doc -> structural changelog, headline withheld
    and stated).

Slice 1 ships a correct, deterministic, honest-about-its-own-coverage changelog
on top of ONLY existing differs. It is releasable without Slice 2.

**Slice 2 - close the audit-surface gap (hardening item 64).**

This slice HARDENS item 64's `audit_diff` foundation, which today differences only
crossings (`diff_crossings`) and reach (`diff_reach`). The new differs belong in
`audit_diff.py` (the `evaluate` gate reuses them too), NOT in `changelog`; they
pay down the item-64 debt this feature exposed (see the Revision PO note), and
each closes one blind spot the revised guard was honesty-lining in Slice 1.

- `src/revl/audit_diff.py`: `diff_capability_scopes(prev, new)` (the CRITICAL:
  `boundary[*].capabilities` label->scope maps), `diff_backends` (the CRITICAL
  second instance: `externs[*].backends`, a new host body is a new reachable
  crossing), `diff_recovery` (`recovery_surface`: a dropped inverse/compensation),
  `diff_registers` (`capability_registers`: a weakened idempotency register), and
  `diff_cardinality` (`cardinality`: a raised ceiling). Each returns sorted
  added/weakened/tightened buckets, mirroring `diff_crossings`/`diff_reach`.
- `changelog.classify` folds them in; each moves from unclassified to its class
  (dropped inverse / weakened register / raised ceiling / widened scope / new
  backend host body -> breaking `lede=True`). Extend `CONSUMED_PATHS` to the
  leaves each new differ now reads, so the guard test still asserts the
  differ<->path correspondence.
- Tests: each new differ's soundness, and the Slice-1 honesty-line regressions
  (capability-scope, new-backend, dropped-recovery) flip from
  "surfaced as unclassified" to "classified breaking" (and the headline from
  non-clean to a definite level).

**Slice 3 - registry attach + formats (item 49).**

- `registry.py` stores the derived changelog on publish and headlines the
  item-64 bump; a publish whose declared version contradicts the computed one is
  already refused by item 64 - the changelog rides that same computation.
- `--format markdown|json|plain` and a stable Markdown skeleton for release
  tooling.

Order is strict: Slice 1's completeness guard makes Slice 1 SAFE to ship before
Slice 2 exists, because it can never silently drop what Slice 2 will difference.
