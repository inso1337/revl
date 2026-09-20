# 562: one named framework benchmark, with the refusal column leading

Roadmap item 548. Issue #1267.

`bench/` held most of a benchmark and none of a published one: 30 component
specs, per-variant prompts for v1 / v2 / v2host and for raw Cordis TypeScript,
a residue probe over a real cordis runtime, an admission-latency runner, a token
accountant, and the frozen EVAL-1 honesty protocol with a checker that enforces
no-self-score, named gates and a claim ladder. What was missing was a named
comparison an outsider can run: same tasks, same model, different runtimes.

This note records what was built, the three decisions that were argued over, and
what the first report deliberately leaves empty.

## 1. Why not a model benchmark

SWE-bench, HumanEval, LiveCodeBench and the agent-success leaderboards rank
models. This project's claim is the opposite one: **the model can be mediocre
and the gate still refuses residue, capability widening and unmarked
emissions.** A high HumanEval score is orthogonal to that claim.

The sharper point is that a high score on a compile-rate leaderboard would
falsify it, because the cheapest way to raise compile-rate is to loosen G1-G9.
A benchmark whose best achievable result is obtained by removing the thing being
benchmarked is not measuring the thing. The discriminating axis is the runtime.

## 2. The shape

One model, one task set, three hosts.

| host | scored on | why |
|---|---|---|
| raw Cordis / TypeScript | leaks after N load-unload cycles | TypeScript always compiles |
| a popular agent framework | leaks after N load-unload cycles | same reason |
| revl | admission, and what it refuses | the gate is the thing under test |

Columns: **refused** (leading), admits, residue, injection escape, tokens to
green, admission latency.

### The asymmetry is stated, not hidden

The raw-TypeScript host has no first-pass compile number, because the number
would read 100% on every attempt including the ones that leak on unload. So one
host is scored on what it leaks and another on what it admits. Those are
different questions, and a reader who compares the two cells directly is
comparing the questions rather than the runtimes.

`bench/hosts.json` carries that sentence in the host's own record and
`report.md` prints it under the table. It is the finding the raw row exists to
state; a benchmark that quietly printed `100%` in that cell would be reporting
a fact about TypeScript's type checker as though it were a fact about lifecycle
correctness.

## 3. The refusal column, and why it leads

Every other column is one revl is designed to win. A suite whose author picked
the axes is benchmaxxing regardless of intent, and adding one column revl loses
does not repair it, because the author also picked that column.

What repairs it is publishing the cost of the guarantee as the headline: the
documents and constructs this project refuses that the other runtimes run
without complaint. A bench that ships its own residual list is trustworthy in a
way one that ships four wins is not.

**If the finished table shows revl winning every column, that is a finding about
the table, not a result.**

### The column is recomputed, never transcribed

The first draft of this work was going to quote the residual figures out of the
roadmap. That would have been wrong in a way worth recording, because the
roadmap, `docs/selfhost-compile.md` and `docs/selfhost-findings.md` currently
hold **three different totals for the same quantity**, none of which matches
what the tests gate:

| source | total residual | kind |
|---|---:|---|
| `docs/v2.0-roadmap.md` item 146 | 59 | prose, written 2026-09-18 |
| `docs/selfhost-compile.md` | 63 | prose, with a 239-document table |
| `docs/selfhost-findings.md` | 63 | prose |
| `tests/test_selfhost_compile.py` `LOWER_GAP_DOCS` | **41** | gated by a test that recomputes it |

So `bench/refusal_inventory.py` reads the ledgers the tests gate and recomputes
every count at report time. A number recomputed from the ledger cannot disagree
with the tests, because it is reading what the tests read. A number typed into a
document is a snapshot of the day somebody typed it.

Four sources, each with the gate that fails when it drifts:

- **native-chain residual**: `LOWER_GAP_DOCS` in `tests/test_selfhost_compile.py`,
  documents the reference compiler accepts and the fully-native chain does not
  reproduce, per tier, against that tier's corpus size. Gate:
  `test_the_residual_is_located_in_lower_not_in_the_emitter`, which pins the set
  in both directions.
- **unported constructs**: `tests/fixtures/selfhost_blind_spots.json`. Gate:
  `tools/selfhost_coverage.py --check`.
- **unreached dispatch arms**: `tests/fixtures/oracle_construct_reach_ledger.json`.
  Listed separately and labelled weaker, because an unreached arm is untested
  rather than known-broken. Gate: `tools/oracle_construct_reach.py --check`.
- **the fail-open direction**: `tools/gate_reference_census_baseline.json`,
  programs the embeddable gate admits that the reference refuses. Published for
  the same reason as the rest: a table of refusals that omitted the places the
  gate is too permissive would be making the selective argument it claims to be
  correcting. Gate: `tools/gate_reference_census.py --check`.

A source that will not read is reported as unavailable, never as zero. A zero
that means "the ledger would not parse" is indistinguishable in a published
table from a zero that means "there is nothing left to refuse", and the second
is a far stronger claim.

## 4. Pinning the model

An API model drifts under a fixed name. A reader who reruns the suite against
"the same model" six months later is not running the same model, and the table
re-baselines without anybody deciding to re-baseline it. Local weights have a
content digest.

`bench/model_pin.py` probes the endpoint and copies the identity out of it: id,
digest, parameter size, context length, family, and quantisation. It records
quantisation **twice**, because the two answers disagree: ollama reports
`unknown` for a GGUF it did not convert itself, while the tag the operator
pulled names `Q4_K_M` explicitly. Recording only the endpoint's answer loses the
string a reader needs to pull the same weights; recording only the tag asserts
something the endpoint did not confirm.

It also records the name the tag resolved from. The roadmap and the issue write
the model as `ornith-ai/Ornith-1.5-35B-A3B:Q4_K_M`; an ollama pull from Hugging
Face registers it as `hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M`. Same
weights, different string, and a pin that silently resolved one to the other
would be reintroducing the drift it exists to stop. The resolution is recorded
as `exact` or `normalised`.

Throughput is measured rather than quoted, with n, a standard deviation, and the
first sample flagged as cold rather than discarded. On ollama the figure comes
from `eval_count / eval_duration`, which excludes model load; a cold load of a
22 GB weights file otherwise dominates wall clock and has nothing to do with the
model's throughput.

## 5. What the first report leaves empty, and why that is the design

A cell is a number or it is `not-run`. There is no third state, and in
particular there is no state where a cell headed by the pinned model's name
contains a number produced by a different model.

The first committed report therefore leaves these empty, each naming what it is
blocked on:

- **the third host.** No agent framework is named. Naming one badly produces a
  dishonest column: a framework with no unload path scores maximally badly on
  residue by construction, for a reason that says nothing about the runtime
  comparison. `bench/hosts.json` carries the selection criteria instead of a
  name.
- **the injection-escape column.** `docs/prompt-injection-resistance.md` states
  the claim and carries no runnable check; the repository's own doc inventory
  grades it `needs-work`, and nothing in `tools/` or `tests/` references it by
  name. `tests/test_adversarial_gate.py` exists and is an executable attack
  suite, but it belongs to `docs/threat-model.md` and is not an escape rate over
  these 30 briefs. Putting it in the cell would be answering a different
  question under the column's name.
- **a pinned-model run.** No cell in the first report was generated by the
  pinned model. The admission and token cells are re-scores of corpora generated
  by other models, and each says so in the cell rather than in a footnote.
- **independent reproduction.** Every claim stands at the ladder's `measured`
  rung and none at `demonstrated`, because `demonstrated` requires a
  reproduction by a party that is not the generator.

The residue number carries its own disclaimer in the report: the hand corpus is
ten plugins this repository wrote, six clean and four leaky, so 4/10 shows that
the probe detects the leaks we planted. It is not a population rate, and it is
exactly the kind of result the issue predicted would be dismissed, rightly,
until somebody who is not us runs the harness.

## 6. Files

- `bench/hosts.json`: the registry an outsider edits: hosts, pinned model, task
  set, columns, and a `who_wrote_what` block naming the prompts this repository
  authored.
- `bench/refusal_inventory.py`: the refused column, recomputed from gated
  ledgers.
- `bench/model_pin.py`: the pin, probed rather than asserted.
- `bench/framework_bench.py`: assembles the report, emits EVAL-REPORT-1 JSON
  plus the markdown table, and validates itself with
  `tools/check_eval_report.py` under `--check`.
- `bench/results/framework-bench/`: the committed artifacts, raw beside the
  summary.
- `tests/test_framework_bench.py`: the tests.

Nothing is published outside this repository. Publication is a named remaining
gate in the report, not a step that happened quietly.
