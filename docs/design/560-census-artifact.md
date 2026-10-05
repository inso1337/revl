# 560. Publishing the gate/reference census as a generated artifact

Roadmap item 560, issue #1268.

## The claim worth publishing

`tools/gate_reference_census.py` runs two independently written implementations
of revl's semantics over the same corpus. `src/revl/*.py` is the reference
compiler. `selfhost/*.rvl`, compiled into `crates/revl-gate`, is the self-host
gate. The census classifies every disagreement, and agreement means the same
TAG and the same MESSAGE, not merely the same verdict.

The publishable property is not the corpus size. Any project can grow a corpus.
It is that the bucket that matters **cannot be written**.

`false-admission` is the gate issuing an admission for a program the reference
refuses: telling a host the reference said yes when it said no. That bucket sits
in the census's `NEVER_BASELINED` tuple, and that has two consequences no
re-record can undo:

- `--record` drops it on the way into the baseline, so no run produces a
  baseline that grants one tolerance;
- `--check` fails on any member regardless of what the baseline says, including
  a baseline hand-edited to list it.

Every benchmark is cooked the same way: by adjusting what counts as acceptable.
Here that adjustment is not available in the direction that matters. That is a
rarer claim than any leak table, and until this item it was visible only to
people reading this repository.

## Why a generator and not a document

A published table whose numbers were typed by a person is a mirror, and a mirror
rots against the thing it mirrors. This repository has hit that shape enough
times to stop arguing about it.

So the report is OUTPUT. `tools/census_artifact.py` is the only thing that
writes it, every count and every named residual comes from a run, and `--check`
reports a hand edit as drift.

### Records, not the report (issue #1768)

The repository first committed the rendered report, `docs/census-artifact.json`
and `docs/census-artifact.md`. Every pull request that added a program or
touched a module the census opens rewrote the same lines of both: the run id and
the compiler digest six times each, `n`, the bucket counts, the claims and most
of the markdown. Each landing therefore put nearly every other open pull request
in conflict there, and resolving it meant another census run that the next
landing undid.

What is committed now is the records of the run, in `docs/census-artifact/`:
`cases/` (one file per program, at a path that mirrors its case id; see
`docs/census-artifact/cases/README.md`), `pins.jsonl` (one `[group, file]` row
per pinned file, sorted) and `facts.json` (the engine, the issued admissions, the
reference faults and the driven probe). One record per line in the two record
files, a blank line between records, nothing derived. The cases began as one
sorted `cases.jsonl`, and two pull requests that each added programs at the
same place in it still conflicted (issue #1768); a file per program removes
that. Not even the
digests are stored: the sha256 of a program or of a pinned file is a property of
the checkout, and storing it meant every pull request that edited a pinned
module rewrote its line even when no verdict moved. Every aggregate is a function
of the records and the checkout: `run` is a sha256 over the `(case id, source
sha256)` rows and the compiler digest one over the `(module, sha256)` reference
pins, so both are computed when the report is rendered, and a published copy
(`--json`) still pins every input by digest. `--from-records` renders it with no
census run, and `test_a_report_rendered_from_records_is_the_report_a_run_builds`
holds that the report is the same either way. `docs/census-artifact.md` is now a
hand-written page with no number in it.

Two pull requests now conflict in the records only when both moved the verdict
of the same program or changed which files the run reads, and the resolution is
the same command as the regeneration: `python3 tools/census_artifact.py
--write`. `--verify --strict` also compares each record file and each case
file byte for byte against the run, and reports a missing or left-over case
file, so a reordered, hand-merged or forgotten file fails it.

## The mechanism is driven, not asserted

The artifact's central claim is about what the code refuses to do. A claim of
that shape is worth exactly what its demonstration is worth, so the generator
does not restate the paragraph above in prose and publish it. It hands both
halves of the mechanism a synthetic `false-admission` member and records what
they did:

- the record half: `record_payload` is given a census whose only finding is the
  synthetic member, and the payload it returns is searched for it;
- the check half: `compare` is given that member **and** a baseline that lists
  it, which is the most generous baseline that could exist, and the problems it
  returns are searched for the refusal.

A run in which either probe came out the other way writes a report that says so,
in the table, rather than a report that quietly claims the property anyway.
`tests/test_census_artifact.py::test_the_probe_reports_a_failure_rather_than_hiding_it`
drives the probe against a stand-in whose `NEVER_BASELINED` is empty and holds
that it reports `holds: false`, because a probe that can only say yes measures
nothing.

This needed one refactor. `--record`'s payload was built inline in
`gate_reference_census.main`, so the `NEVER_BASELINED` filter was unreachable
without rewriting the committed baseline. It is now `record_payload`, and
`--record` writes byte-identical output across the change.

## The allowance is published as it stands

`false-admit` is the other direction: the reference refuses under a guarantee the
gate claims to decide, and the gate raises no objection. It is a bypass, it is
baselined, and it is not zero. The artifact names every residual rather than
counting them, for the same reason `tests/test_gate_reference_census.py` keeps
`KNOWN_BYPASSES` as a named list: a count can churn unread, a name has to be
edited in a diff somebody reads.

Published as it stands on the day, not as it will stand once open work merges.
Issue #106's work closes this allowance and had not merged when the artifact was
generated, so the artifact carries the real number and
`test_the_published_residuals_are_exactly_the_baselined_ones` reds the day it
stops being the real number.

## Identity by content digest, not by commit or clock

Nothing in the artifact is a timestamp or a git sha.

An artifact stamped with a wall clock or a commit reds on every unrelated
landing, and `tools/docgen.py`'s own reasoning about the roadmap applies here:
a gate that fails constantly gets routinely bypassed, and a bypassed gate is the
failure it exists to prevent.

So identity is content-addressed. `run` is a sha256 over the corpus the run
actually read. `checker_version` is a sha256 over the files that decide what the
census does. `compiler_commit`, which `EVAL-REPORT-1` requires by that name,
holds a sha256 over the `src/revl/**/*.py` modules the census run opened (the
set in `census.pins.reference`; it was the whole glob until issue #1572), and the
report says in as many words that it is a tree digest and not a commit. Each is narrower than a commit sha, because
a commit that moved none of those files moves none of them, and each is
recomputable by an outsider from a checkout.

## The reproduction, and what it does not establish

The fast engine is a python mirror of the native gate's guards, so it could in
principle be wrong in the same direction as the thing it mirrors.
`--engine crate` asks the real `crates/revl-gate`, built by cargo, the same
questions. Measured for this artifact: 848 programs, the same 9 false-admit
residuals, zero false admissions, agreement on every tracked bucket.

That run takes about fourteen minutes and needs a rust toolchain, so `--check`
cannot run it. It is recorded in `tests/fixtures/census_crate_reproduction/`,
one file per checker version it was taken at: `<checker version>.json` holds the
version, the tracked buckets, the false admissions and the programs the run
covered (issue #1768). One file holding the version was the line every pull
request that moved a checker source rewrote, so two of them always conflicted
on it; now a re-record ADDS its version's file, and two re-records add two
different files, which git merges. A re-record does not delete the old record,
because a delete plus a near-identical add reads to git as a rename and two
renames of one file conflict; `--prune-stale-reproductions` deletes stale
records in a change of its own. The program count is derived, and a census
program the reproduction did not run is counted in the report. After two
re-recording pull requests land, no record is at main's checker version until
one re-record there (`python3 tools/regen_generated.py --only census`), and
`--verify --strict` names it until then. A recorded result rots, so it is not trusted blind: every run
compares the recorded version against the current one, and a reproduction taken
at a different version is published as stale and **lifts no claim**. The ladder
rung in the report is computed from evidence that is current, never declared.

The limit is stated in the artifact itself. The crate is built from
`selfhost/lower.rvl` by `tools/build_gate_crate.py`, so it is not a second
specification: it is the same rvl source through a different emitter, toolchain
and runtime. What the reproduction rules out is the python mirror being wrong.
The independence that carries the census is the other axis, `src/revl` against
`selfhost/`, and that independence lives in the measurement rather than in the
reproduction. Publishing the crate run as an independent reproduction without
that sentence would over-claim, and
`test_the_reproduction_states_its_own_limit` holds the sentence in place.

## The honesty protocol

The report (`python3 tools/census_artifact.py --json`) is an `EVAL-REPORT-1` document under the frozen
protocol in `docs/design/478-eval-honesty-protocol.md`, and
`tools/check_eval_report.py` passes on it. That checker decides three things:
`noSelfScore`, that every brief names a gate from the frozen set, and that no
claim stands higher than the rung its own evidence justifies.

`noSelfScore` is not ceremony here. The thing being graded is the self-host
gate, and the grader is the reference compiler, which never sees the gate's
answer. The two identities are disjoint, and the census would be worth nothing
if they were ever the same party.

There is one brief, deliberately. The frozen gate set (`compiles`,
`pinnedInterfaces`, `noResidueForRawBaseline`) is defined for bench specs in
`bench/specs.json`, and `compiles` is the only one of the three that means
anything over a differential census corpus: the 463 programs the reference
admits compile under the current checker with no error and no compiler crash.
Stretching the other two to fit would be exactly the over-claim the protocol
exists to stop, so the census results that have no frozen gate are in the
`census` section and in the claims, rated on the ladder instead of dressed as a
gate they are not. `GATE-CENSUS-1` names that section;
`tools/check_eval_report.py` neither reads nor constrains it.

## What CI gates, and when (issue #1572)

**Superseded 2026-09-29.** The section below this one explains why the check was
first left out of CI. It is kept as the record of that decision, which turned
out wrong in practice: with no step running it, the committed artifact drifted
from main (42 programs behind, with one deciding file and 29 reference modules
moved and 2 added) and nothing reported it. A check nobody runs reads as a pass.

The decision now is option (a) of issue #1572. CI's `census-artifact` job keeps
the committed copy current, and it runs only when a pull request moves an input,
so an unrelated branch is not interrupted:

1. `tools/census_artifact.py --moved-inputs` reads the pull request's changed
   paths and names the ones that are inputs of the committed artifact: a file in
   its pins, a program it carries, a new `.rvl` under a census directory, a gate
   crate source, the declared checker and report inputs, and the tool and the
   artifact themselves. None named: the job says so and checks nothing. Push to
   main, schedule and dispatch always check.
2. `tools/census_artifact.py --verify --strict` re-runs the census (about 20
   seconds) and judges the committed file the way a reader's `--verify` does,
   then also fails when a program in the corpus is missing from the file or a
   report input moved. A reader's `--verify` tolerates both, because a reader's
   clone is expected to have moved; the repository's own copy is not.
3. A pull request that moves an input regenerates the artifact in the same diff,
   `python3 tools/census_artifact.py --write`, the way a golden is regenerated.
   Its bytes are then what `--check` produces too.

Two changes made this practical. The reference pins were the whole
`src/revl/**/*.py` glob, which made every compiler change an input: of the 19
`src/revl` files that changed on main between PR 1469 and `67fc027b7`, one is
a module the census run opens. They are now measured by the same audit hook as the
deciding files (38 of 192 modules on `67fc027b7`), which is sound because the
fast engine runs in process, and a run that opens no module under `src/revl`
(revl imported from outside the checkout) is refused rather than pinned as a run
over no reference. The recorded crate reproduction still has to be refreshed by
hand when `checker_version` moves, because it needs cargo and takes minutes;
`test_a_stale_reproduction_lifts_no_claim` is what says so.

## Why the check was first left out of CI (superseded)

`tools/census_artifact.py --check` was **not** wired into a CI job, and that was
a decision rather than an omission.

The check compares the committed artifact against a fresh run, so it reds
whenever the corpus grows. This repository lands parallel work continuously and
a corpus document arrives often, so wiring it in would red unrelated branches
for a reason that has nothing to do with them. The regeneration is cheap, but
the interruption is not, and a gate everyone learns to bypass protects nothing.

What is gated instead is the number that can be *wrong* rather than merely
*behind*: `test_the_published_residuals_are_exactly_the_baselined_ones` holds the
published allowance against the committed baseline. It reads two files, runs no
census, and reds exactly when the standing allowance moves, which is the claim a
reader would quote.

The residual risk is stated rather than hidden: between corpus landings, the
published `n` can lag the tree. `--check` is the pre-publication gate, run by
the person publishing, and publication is a human step anyway.

## Pins and a verifier: closing the cooking direction for a published copy

Issue #1268 asks for an artifact whose cooking direction is closed by
construction. Everything above closes it inside the repository. A copy that has
left the repository needs three more things: a fixed corpus, a gate pinned by
digest, and a verifier a reader can run against the copy they hold.

### What was missing

`--check` answers "is the committed file what today's tree produces". It fails
the moment the corpus grows, and on the tree this section landed on it was
already failing on `main`: 894 programs published, 931 in the tree. So a reader
holding a published copy a week later has no way to tell "the numbers were
wrong" from "the tree moved". Both print the same drift line.

The identities were also incomplete. `checker_version` digests five named files.
Measured by recording what a census run opens, two more files decide verdicts
and are in neither the checker version nor the reference digest:

- `backends/python/emit.py`, which turns `selfhost/lower.rvl` into the python
  the fast engine executes;
- `tests/test_selfhost_lower.py`, whose `_classify` maps a reference error to
  the tag the census compares, and which also holds the inline oracle programs.

Either could change a published verdict without moving any published identity.
`checker_version` is left as it is, because it is the key the recorded crate
reproduction is stored under, the crate engine reads neither file, and adding
`tests/test_selfhost_lower.py` would make the key move with every oracle program
somebody adds.

### What the artifact now carries

- `census.cases`: one row per program run, in run order: case id, sha256 of
  the source bytes, bucket. A repeated case id gets one row per run.
- `census.pins.decides_verdicts`: every file under the checkout that the census
  run opened, minus the corpus and the reference, each by sha256. It is
  **measured** through a Python audit hook (`sys.addaudithook`, event `open`),
  not listed, so it cannot be incomplete by omission. A byte-compiled read is
  pinned as its source.
- `census.pins.reference`: the `src/revl/**/*.py` modules the run opened, per
  file, measured by the same hook (issue #1572; it was the whole glob before).
- `census.pins.report_inputs`: the baseline, the provenance manifest, the
  recorded crate reproduction and the shipped crate sources. They shape the
  report, not any verdict, so a change to one is named and never reads as a
  refutation.

### The verifier

`python3 tools/census_artifact.py --verify [REPORT]` re-runs the census in the
reader's clone and judges the published file in two steps: pins first, then
verdicts case by case on every published program whose bytes are unchanged.

| verdict | exit | when |
|---|---|---|
| reproduced | 0 | inputs that decide a verdict identical, every published row matched |
| refuted | 1 | same inputs and a row differs; or the file contradicts itself (the bucket table is not the sum of its rows, or it lists a false admission); or the local run has a false admission; or `NEVER_BASELINED` does not hold |
| unusable | 2 | the file cannot be read, or predates rows and pins |
| partial | 3 | same inputs, every row still present matched, some programs edited or removed |
| different-inputs | 3 | a deciding file moved, or the local run opened a file the publication does not pin (reported as UNPINNED INPUT) |

The order matters. Pins are compared before verdicts so that a difference in the
inputs is never reported as a difference in the result, which is the failure
`--check` has. New programs in the reader's tree do not stop a reproduction:
the published rows are checked and the new ones are listed.

`judge` is pure and every arm above has a test in `tests/test_census_artifact.py`
that drives it with synthetic rows. Run end to end on the tree this landed on
(931 programs, 925 distinct), against the committed file and three tampered
copies:

| input | verdict | exit |
|---|---|---|
| the committed file | reproduced, 931 of 931 rows | 0 |
| one row moved from `agree-refuse/G4` to `agree-admit`, table adjusted to match | refuted, the case named | 1 |
| the pin for `backends/python/emit.py` changed | different-inputs, the file named | 3 |
| the pin for `tests/test_selfhost_lower.py` deleted | different-inputs, UNPINNED INPUT named | 3 |

### Which cooking moves the construction closes, and which it does not

| move | what stops it | closed by |
|---|---|---|
| record a `false-admission` into the baseline | `--record` drops it | the tool |
| hand-edit the baseline to tolerate one | `--check` fails on any member | the tool |
| edit a count in the published JSON | it must equal the sum of the rows; `--verify` recomputes every row | the tool |
| edit a row and the count together | `--verify` recomputes the row from pinned inputs | the tool |
| measure with one gate, emitter or classifier and publish another | measured pins; the reader's run names what moved or was never pinned | the tool |
| quote the fast engine where the crate disagrees | the crate run is keyed by checker version and a stale one lifts no claim | the tool |
| drop hard programs from the corpus before publishing | the corpus is globbed from fixed directories, not selected; a removal is a public diff | public history |
| bend the reference until it agrees with the gate | the bent reference is the pinned one, readable by anyone | public history |
| mislabel a document's provenance | nothing | public history |

The first six are closed by construction: no edit to the published JSON
survives `--verify` on its own pinned inputs, and no baseline can tolerate a
`false-admission`. The last three are not, and the artifact says so rather than
implying otherwise. Their only defence is that the repository is public and each
of them is a diff.

The audit hook has one blind spot, stated here because it is structural: a
subprocess. The crate engine builds and runs `crates/revl-gate` through cargo,
which the hook cannot see, so the crate sources are pinned by glob in
`report_inputs` and tied to `selfhost/lower.rvl` by
`tools/build_gate_crate.py --check`, not by measurement.

## What this item does not do

It does not publish. It writes files into this repository and stops. Sending the
artifact anywhere outside the repository is a separate decision, and the tool has
no code path that reaches a network.
