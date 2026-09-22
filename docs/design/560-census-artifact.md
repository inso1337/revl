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

So `docs/census-artifact.md` and `docs/census-artifact.json` are OUTPUT.
`tools/census_artifact.py` is the only thing that writes them, every count and
every named residual comes from a run, and `--check` reports a hand edit as
drift. The file says so in its first paragraph, where a person about to edit it
will see it.

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
holds a sha256 over `src/revl/**/*.py`, and the report says in as many words that
it is a tree digest and not a commit. Each is narrower than a commit sha, because
a commit that moved none of those files moves none of them, and each is
recomputable by an outsider from a checkout.

## The reproduction, and what it does not establish

The fast engine is a python mirror of the native gate's guards, so it could in
principle be wrong in the same direction as the thing it mirrors.
`--engine crate` asks the real `crates/revl-gate`, built by cargo, the same
questions. Measured for this artifact: 848 programs, the same 9 false-admit
residuals, zero false admissions, agreement on every tracked bucket.

That run takes about fourteen minutes and needs a rust toolchain, so `--check`
cannot run it. It is recorded in
`tests/fixtures/census_crate_reproduction.json` beside the checker version it was
taken at, and a recorded result rots, so it is not trusted blind: every run
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

`docs/census-artifact.json` is an `EVAL-REPORT-1` document under the frozen
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

## What is deliberately not gated in CI

`tools/census_artifact.py --check` is **not** wired into a CI job, and that is a
decision rather than an omission.

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

## What this item does not do

It does not publish. It writes files into this repository and stops. Sending the
artifact anywhere outside the repository is a separate decision, and the tool has
no code path that reaches a network.
