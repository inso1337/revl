# How work moves

The short version: **agents run targeted tests, push a branch, and open a PR.
GitHub CI runs the full matrix. The orchestrator merges on green.**

## Why this exists

Agents used to run the whole suite locally before reporting. A full pass is the
frontend suite plus the cordis suite plus the per-backend emit suites, and it
costs ten to twenty minutes of wall clock and a large share of the machine.
With several agents working at once the machine was the bottleneck, and worse,
concurrent runs are not trustworthy: `tests/test_network_placement.py` binds
fixed ports 39555-39561 and `tests/test_network_gate_path.py` spawns a real node
over loopback, so two agents running suites at the same time produce reds that
belong to neither of them. Everyone waited, and the result was not even reliable.

CI runs the same matrix on clean, isolated runners. That is where the full suite
belongs.

## What an agent does

1. Work on a branch named for the issue: `fix/<issue>-<slug>` or
   `agent/<issue>-<slug>`.
2. Run **only the tests covering what you changed.** If you touched
   `backends/rust/emit.py`, run the rust emit suite and the rust golden tests.
   Do not run the root `tests/` tree. Do not run the cordis suite.
3. Commit and push the branch.
4. Run the cheap pre-PR checks. They take seconds and catch most `lint` reds:

   ```
   ./tools/pre_pr.sh
   ```

   Seven checks: ruff; a Python 3.11 syntax sweep (CI runs 3.11, the dev venv
   is newer, so 3.12+ syntax passes locally and reds CI); the workflow
   permission gate (`tools/check_workflow_permissions.py`, self-test then
   `--strict`); the roadmap marker gate; drift checks on both generated gate
   crates, `crates/revl-gate` (`tools/build_gate_crate.py --check`) and
   `crates/revl-gate-wasm` (`tools/build_gate_wasm.py --check`); and the
   source-derived doc blocks (`tools/docgen.py --check`). It does not run any
   suite.

   A red on either gate-crate check is yours to fix before the PR, not the
   orchestrator's at merge: run the matching builder without `--check` and
   commit the regenerated crate. A red on the docs check is `make docs-gen`
   when a generated block is stale, or a missing section to write when a
   coverage check names an undocumented subcommand or MCP verb — never a hand
   edit inside a `<!-- docgen:KEY -->` region, which the next generation
   overwrites.

5. Open a PR that closes the issue:

   ```
   gh pr create --repo inso1337/revl --fill --body "Closes #<issue>"
   ```

   Use `Closes #<issue>` ONLY when the PR closes the whole issue. GitHub
   auto-closes on merge, so a partial fix written that way silently closes an
   issue whose remaining findings nobody is now tracking, which is the exact
   failure this process exists to stop. For a partial fix write
   `Part of #<issue>` and say in the body which findings remain open.

6. Report what you changed, what you ran, and the PR number. Then stop.
   **Do not wait for CI.** The orchestrator watches it.

## What an agent does not do

- Do not run the full suite locally. That is CI's job now.
- Do not block waiting on a background suite. Report and stop.
- Do not fix a red you did not cause. A pre-existing failing test or a red CI on
  main belongs to the orchestrator. Say you saw it and leave it alone.
- Do not merge your own branch.

## Changing logic that `selfhost/*.rvl` mirrors

`selfhost/emit_*.rvl`, `selfhost/lower.rvl` and `selfhost/checker.rvl` are ports
of `backends/<tier>/emit.py` and `src/revl/`. The oracles in
`tests/test_selfhost_*.py` hold the two sides to byte agreement over a corpus.

**A green oracle is not evidence about a construct the corpus never spells.** It
catches divergence, and only on inputs the corpus reaches. It cannot tell you
which side is right when both agree and both are wrong, it cannot demand a fix
on a case the corpus never reaches, and it cannot see that the two sides mirror
different source-of-truth sets. Item 429 records five same-day defects of those
kinds, two of which the SELF-HOST had right and the reference had wrong.

So, whenever you change logic that a `selfhost/*` file mirrors:

1. **Open the self-host file and read the mirrored function.** Directly. Do not
   infer its state from a green oracle, and do not infer it from a sibling port
   that already landed. Item 429(c) is a rule that was ported while its
   source-of-truth SET was not, which reads as done at a glance.
2. **Say in the PR body what you found there**: ported, already correct, or a
   gap you are leaving open and why.
3. **If the two sides disagree, decide which is right.** Do not assume it is the
   reference. It lost twice in one day.
4. **Add the corpus case before the fix, and watch it FAIL.** A corpus entry
   that was never seen red proves nothing.

Two gates measure what the oracle's own corpus does not reach and refuse to let
that set grow silently:

* `tools/selfhost_line_coverage.py --check`, exercised by
  `tests/test_selfhost_line_coverage.py`, measures unexecuted STATEMENTS on both
  sides. The port is measured through its emitted Python, attributed to the
  declaring `.rvl` function, not to `.rvl` source lines. Its totals exclude
  generated scaffolding; optimizer-inlined helpers can retain unentered
  definitions even when their inlined behavior runs. Run the tool for current
  counts; historical percentages are not a coverage target.
  Three responses to a finding, in order of preference. A corpus document that
  reaches the statements. A document in `tests/fixtures/emit_<tier>_refusals/`
  when the only thing that reaches them is one the tier's reference REFUSES, so
  no corpus document ever can: a corpus document is one the reference emits, and
  both halves of a named refusal run only where it does not. Deleting the
  statements when nothing reaches them at all. Recording the count is the last
  resort, and it costs a raise to `_budget` in the ledger, per half and tier,
  which `--write` does not write and the gate holds the recorded mass to
  exactly. Issue #1419 is why: the ledger does fall (21 of the 88 changes to it
  lowered the mass, and the 2026-09-05 triage took 2234 out in five commits),
  but nothing bounded it, so `--write` plus a written reason was a complete
  answer to the gate firing.
* `tools/selfhost_coverage.py --check` is the cheap construct-level check over
  dispatch arms, exercised by `tests/test_selfhost_coverage.py`. It names
  constructs rather than functions, but reaching a dispatch arm does not mean
  executing its whole body.

Both are floors, not substitutes for the rule above. Statement coverage can find
unexecuted code inside a reached arm, but neither gate detects a missing member
of a reserved-name set on a line that already executes. Neither establishes
that covered code is correct.

Triage is not a baseline refresh. Add oracle cases for supported omissions and
fix demonstrated divergences; for remaining gaps, record the actual source
condition and evidence for a declared scope boundary or unreachable path.
An unentered function or a partially exercised function is a measurement, not
an explanation. Do not declare a supported feature out of scope merely because
its new corpus case fails. A justified parity deferral remains a limitation,
not an assertion that the oracle covers that feature.

Use the differential survey to reproduce a broader repository experiment without
changing either ledger or corpus:

```sh
python3 tools/selfhost_differential_survey.py --tiers ts java --select-cover --json survey.json
```

`--documents path/to/input.rvl` narrows the experiment.
The report distinguishes frontend/reference rejection, port refusal or error,
byte divergence, and byte agreement. Its greedy suggestions use additional
statement reach on both sides, accept only agreeing inputs, and measure against
the oracle's actual `CORPUS` list. Baseline failures remain explicit in the
report. WASM compares only the `functions` projection: mixed-component documents
remain in the report but are excluded from automatic selection, since work on
discarded component output is not evidence of agreement.
Review suggested inputs and remaining source branches; a zero-exit
survey, byte agreement, or a coverage gain is not proof of correctness.

## Declaring a closed vocabulary in a second module

The self-host rule above is about two implementations of one semantics. The same
thing happens one level down, inside the compiler's own helpers, and until issue
#1285 nothing watched it: a set or mapping over a closed vocabulary gets
re-declared in a second module rather than imported. Each copy is correct when
written. They drift. Nothing reports the drift, because the only thing that
would notice is a consumer comparing two of them, and no consumer does.

Three instances were found by three unrelated lanes in one day, none of them by
a gate: two independent surface-type to JSON Schema mappings (issue #1272), three
copies of the IR path normalization an attestation binds, two of which had
already drifted and one drift of which had left `truc reproduce`'s attestation
tier structurally dead (issue #1276), and `policy.TAINT_FOLD_ORIGINS` mirroring
`taint._SOURCE_CLASS_SCOPES` where `mcp/approval.py::static_taint` intersects
against it, so a missing entry silently drops an origin from the taint an
auto-approve decision is made against (issue #1195).

`tools/check_vocabulary_mirrors.py` is the gate. It runs in `lint`, is
stdlib-only, imports no revl and starts no subprocess, so it cannot be fooled by
the dev venv's editable meta-path finder into measuring some other checkout.

* **The inventory is produced by the tool**, not kept by hand. Run it with no
  arguments for the current list.
* **A mirror is an exact match.** Two sites mirror each other when the closed
  vocabularies they spell are EQUAL and they live in different modules. A site
  is a module-level collection constant, or a function whose body reads at least
  four distinct string literals as mapping keys. Exact equality has no threshold
  to tune and cannot chain, which the looser relations do: measured on this
  tree, Jaccard at 0.9 over all string literals gives 117 pairs, a strict subset
  relation with a 0.5 size guard gives 424, and the transitive closure of any
  threshold below 0.7 merges 27 to 30 unrelated declarations into one blob
  through the single shared token `rust`.
* **The ledger is a ratchet, not an allowlist.** Every class in
  `tests/fixtures/vocabulary_mirror_ledger.json` carries the sites, the
  vocabulary they agree on, and a written reason, and every entry keeps being
  checked in both directions. A recorded copy drifting reds and names the token
  each side has and the other lacks. A recorded class growing a third copy reds.
  An entry whose mirror is gone reds, and the fix is to DELETE it, which is what
  makes the ledger shrink-only. A new class reds. A missing or unreadable
  ledger reds, and so does a scan that finds nothing.
* **`--write` is not a routine regeneration.** No `make` target and no
  `regen_goldens.py` path calls it. Widening the ledger is an edit somebody
  writes a reason into and a reviewer reads, because a ratchet that widens as a
  side effect of normal work is not a ratchet.
* **It has a measured false-positive rate.** On the tree it was written against,
  42 classes, of which 5 are coincidence rather than re-declaration: several
  readers of one schema that happen to name the same fields, where a correct
  one-sided edit would red the entry with no defect behind it. Those five are
  marked `FALSE POSITIVE` in their `note`. The remaining 37 are real.

`python3 tools/check_vocabulary_mirrors.py --self-test` runs the gate against
each of those failure modes and asserts the verdict. It runs in `lint` beside
the gate, because a checker whose own teeth are never exercised is the gap it
was written to close.

### The near miss exact equality cannot see

Exact equality is blind in one direction, and issue #1336 is the instance.
`tools/evolution_controller.py` carried a third copy of item 536's four verdict
field names and its docstring said so. It had already grown a fifth key, so
equality grouped the two copies that still agreed and never looked at the one
that had left. The rule is strongest against copies still in step and blind to
the one that has already drifted, and the false claim sat where a reader is
most likely to trust it.

Relaxing the relation is the wrong repair, and it was measured rather than
assumed. A strict superset with a bounded difference, at the tightest setting
that still contains that case (one token of difference, a minimum vocabulary of
four), gives **131 ordered pairs over this tree, of which one is the defect**,
and a transitive closure of 27 components whose largest is 12 sites. Two tokens
gives 248 pairs and a 30-site component, which is the blob above rebuilt.
Raising the minimum vocabulary to five drops to 63 pairs but loses the target,
whose smaller side is exactly four tokens. No setting keeps the true positive
and suppresses the other 130.

What works is a different anchor, not a fuzzier relation. A vocabulary that
*says* it mirrors another is a strictly easier case than one that merely
happens to.

* **A claim is prose the gate can check.** A vocabulary site whose own
  docstring, leading comment, or class docstring contains one of a fixed list
  of literal cues AND names a scanned module in backticks is making a claim.
  The claim is SATISFIED when some site in that module spells exactly its
  vocabulary, a NEAR MISS when none does and the closest differs by at most one
  token, and UNANCHORED otherwise.
* **Only a near miss reds.** Unanchored is not a finding. Most prose containing
  "mirrors" is about something that is not a vocabulary, and firing on it is the
  blob by another route.
* **It cannot chain.** A claim is one arrow from a named site to a named module,
  and nothing takes a transitive closure, so the failure mode that rejected
  every similarity threshold does not exist for this rule. There is one knob and
  it is measured: one token of slack gives 1 near miss on this tree, two gives
  2 and three gives 3.
* **Measured on this tree:** 1590 vocabulary sites, 100 of which carry a cue, 28
  of which also resolve a module. Of those 28: 4 satisfied, 1 near miss, 23
  unanchored. The one near miss is recorded with a reason, and it is a false
  positive about the claim while being a true one about the vocabulary:
  `mcp/session.py::Session._live_fingerprint` claims the output shape
  `apply.py::fingerprint` produces, which it does, and the extra token is an
  input key `fingerprint` reads and the session copy reaches another way.
* **The same ratchet.** Near misses live beside the classes in
  `tests/fixtures/vocabulary_mirror_ledger.json`, each with a written reason.
  An unrecorded near miss reds, a recorded one whose difference moved reds, and
  a recorded one that resolves must have its entry DELETED.

## What CI covers

`lint`, `frontend`, `backend-python`, `frontend-cordis`, `sandbox-container`,
`backend-typescript`, `backend-wasm`, `backend-rust`, `gate-wasm`,
`backend-java`, `backend-go`, `backend-roots-combined`, `conformance`,
`temporal-exit`, `formal`. `pull_request` carries no branch filter, so every PR
gets all fifteen. `ci.yml` is also exposed as a `workflow_call`, so the PyPI
publish gates on the same matrix that gates main.

**CodeQL is not part of that.** `codeql.yml` runs on push to main, on a weekly
schedule, and on `workflow_dispatch`. It deliberately does NOT run on
`pull_request`: six language lanes per PR made it roughly half the queue on a
repo whose runner concurrency is the throughput limit, and the required `ci`
workflow was queuing behind scans of code that had not landed. Everything that
reaches main is still scanned. What a PR loses is the pre-merge signal; dispatch
a scan by hand against the branch when a change warrants one.

## What CI does not cover

Anything gated on an env switch that no job sets, and anything needing a
toolchain the runner lacks. `tests/test_env_gated_skips_run_somewhere.py` fails
when a test reads an env name that nothing sets, which is what keeps this list
from growing silently. If your change needs a switch flipped, flip it in
`ci.yml` in the same PR.

## The release path

`publish.yml` triggers on a `v*` tag and on nothing else, so none of it runs on
a PR or on main. A green CI says nothing about it. That gap already cost one
latent break: the publish job declared `id-token: write` alone, and because a
job-level `permissions` block **replaces** the workflow-level one rather than
merging into it, `contents` was `none` and `actions/checkout` could not have
read the tag it publishes (issue #191, fixed in PR #190).

Three things cover it now, and none is a substitute for the others.

`tools/check_workflow_permissions.py` runs in `lint` on every PR. It resolves
each job's effective permission set and checks it against the scopes the
actions in that job need, following local reusable-workflow calls under the
caller's set as a ceiling. It is a static read, so it reaches the publish job
without a tag existing. Its `--self-test` reintroduces the original bug into a
synthetic workflow and asserts the gate catches it, and that runs in `lint`
too. The action-to-scopes table is deliberately small and hand-checked: an
action not in it is skipped with a note rather than guessed at, so add the
entry when you add the action.

`tools/check_wheel_manifest.py` runs in `lint` on every PR, in the release dry
run, and in `publish.yml` immediately before the upload. It asserts that the
built wheel's member list **equals** `git ls-files` of the trees
`hatch_build.TREES` maps into it, plus the `.dist-info` — set equality, so an
over-wide `exclude` is as red as an over-inclusion — and that the **sdist**
carries no file the commit does not track. It exists because the
wheel used to `force-include` `backends/` and `stdlib/` verbatim.
`force-include` runs after file selection and is exempt from every exclude rule
and from the ignore files, so the wheel contained whatever sat on the builder's
disk under those trees: a developer build came out at 2933 members and 250 MB
unpacked against the 523 the commit describes, carrying `node_modules`, cargo
`target/`, a cloned `.cordis-py`, compiled runner binaries and the generated
key material `.gitignore` parks under `backends/typescript/test-secret-store/`.
The size was the visible half; the half that mattered is that the published
artifact was a function of the builder's filesystem rather than of the
revision. `hatch_build.py` now computes that force-include list per file from
`git ls-files` (falling back to `[tool.hatch.build] exclude` where there is no
git, as when a wheel is built out of an unpacked sdist), and the same excludes
took 909 cargo `target/` files and a compiled runner binary out of the sdist.
The gate is deliberately not the same mechanism: it asks git directly, from
outside the build, because the hook and the excludes are exactly what could
drift. Its `--self-test` doctors a built wheel in both directions and plants a
real untracked file under `backends/`, and that runs in `lint` too.

The wheel target keeps `packages = ["src/revl"]`. Remapping `backends` with a
`sources` entry is the tidier spelling and does not work here: a `sources`
rewrite that changes a prefix rather than removing one makes editable installs
impossible, and `pip install -e ".[test]"` is how eleven CI jobs and every
contributor set the repository up.

`.github/workflows/release-dryrun.yml` builds the distribution, runs
`twine check` and the wheel-manifest gate, installs the wheel into a clean
environment and compiles an example with it. It runs weekly, on
`workflow_dispatch`, and on a PR that edits `pyproject.toml`, the manifest gate
or the release workflows. It has no upload step, not even a skipped one. It is
also the only place the built **wheel** is *used* at all: the rest of CI tests
the checkout tree, so a break in what pyproject packages into `revl/backends`
and `revl/stdlib` is invisible everywhere else and would surface as a broken
release on PyPI.

What is still not covered is the publish job's own runtime environment, its
`pypi` environment and the Trusted Publishing OIDC handshake. Those exist only
on a tag build and cannot be rehearsed without publishing. Run
`gh workflow run "release dry run"` and read it green before pushing a tag.

## Merging

The orchestrator merges when CI is green, and regenerates the site wheel if the
change touched it. The gate crates are no longer on that list: `pre_pr.sh` checks
both, so a drifted crate is the branch author's to fix before the PR opens. A
branch is never merged on a local
green alone, because a local green certifies only what actually ran on that
machine at that moment.

That regeneration is no longer only a habit. Issue #252: the committed
playground/site wheel vendors all of `src/revl`, so it is stale after almost any
source change, and it is deliberately not a per-PR gate (as one it reddened CI
on every source change, which is an outage class rather than a defect). The
merge-time assignment above was the whole of its ownership, and under the actual
flow — agents run targeted tests, CI runs the matrix, the pipeline merges on
green — nobody ran it, so the wheel was found stale three times in one day.
`.github/workflows/site-wheel.yml` now runs `tools/check_site_wheel.py` on every
push to main and weekly, and fails on main when the committed wheel does not
match a fresh build. It never runs on a pull request, so no PR is red for not
having rebuilt a deploy artifact. When it goes red, the fix is one command on a
branch:

    python3 tools/check_site_wheel.py --write

The same applies to the generated doc blocks, and for the same reason. Issue
#296: `docs/DOC-STATUS.md`'s `doc-status` block embeds a row per `docs/*.md`, so
**any** landing that touches **any** doc re-stales it. Unlike the site
wheel, `tools/docgen.py --check` *is* a per-PR gate (it runs in the required
`frontend` job). So a stale block on main reddens every open PR at once, for a
reason none of their authors caused. It was already stale at the merge commit
that introduced the gate.

So `make docs-gen` joins the site wheel and the gate crate as merge-time
regeneration the orchestrator owns:

    make docs-gen        # or: python3 tools/docgen.py --write

Regenerate it against the tip that actually lands, not the base the branch was
cut from: a block regenerated an hour earlier is stale again if anything touched
a doc in between. This is the same hazard as merging on a green whose run
predates the last landing: the verdict was true when taken, and false when used.

## Issues are the state

Every unfinished roadmap item has an issue. The roadmap stays the
reasoning-of-record: evidence, cross-references, negative results. The issue
carries the state, so nobody starts the same work twice. Before you begin,
check the issue is not already assigned or already closed.

Security findings do not get public issues. They go to private security
advisories.

## Goldens

Six backends carry checked-in emitter output, and so do `crates/revl-gate` and
`crates/revl-gate-wasm`. They are **snapshot tests, not a freeze**: the invariant is "emitter output
never changes unreviewed", never "output never changes". Regenerating a golden
and reviewing its diff is always an acceptable resolution. Bending an emitter
back to keep old bytes is not.

One command covers the six backends and `crates/revl-gate`:

```
python3 tools/regen_goldens.py             # list every target and the files it owns
python3 tools/regen_goldens.py --check     # which goldens drifted, and the fix for each
python3 tools/regen_goldens.py <target>    # regenerate: python typescript rust java wasm go gate-crate
```

Two generated artifacts are not on that list. `crates/revl-gate-wasm` has its
own builder, `python3 tools/build_gate_wasm.py` (`--check` to test for drift),
and its own gate, `tests/test_gate_wasm_drift.py`. The source-derived doc blocks
have `python3 tools/docgen.py --write` (`make docs-gen`), `--check`, and the
`frontend` job. `./tools/pre_pr.sh` runs all of them, so you do not have to
remember which tool owns which artifact.

Rules:

1. If you change an emitter, regenerate its goldens **in the same commit** and
   review the diff. A separate follow-up commit means main is red in between.
2. `crates/revl-gate` embeds emitted rust. Any change to `backends/rust/emit.py`
   or to `selfhost/*.rvl` rewrites about 8000 lines of it (`src/selfhost.rs`
   alone), and a PR that skips
   the regeneration goes red on drift alone. `python3 tools/regen_goldens.py
   gate-crate` fixes it.
3. A red golden test names the exact command that resolves it. Run that command,
   read the diff, and keep it only if the new bytes are what you meant.
4. Adding a golden means adding it to `tools/regen_goldens.py`. A generated file
   nothing can reproduce is a file nothing can check. `crates/revl-gate-wasm`
   and the docgen blocks are the standing exceptions, and only because each
   arrived with a builder and a CI gate of its own.

The policy and its two declared exceptions live in
[conformance.md](conformance.md), "Golden policy: snapshot, not freeze".
