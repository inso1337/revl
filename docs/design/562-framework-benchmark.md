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
roadmap, `docs/selfhost-compile.md` and `docs/selfhost-findings.md` held
**three different totals for the same quantity**, none of which matched what
the tests gate. Read as of 2026-09-20:

| source | total residual | kind |
|---|---:|---|
| `docs/v2.0-roadmap.md` item 146 | 59 | prose, written 2026-09-18 |
| `docs/selfhost-compile.md` | 63 | prose, with a 239-document table |
| `docs/selfhost-findings.md` | 63 | prose |
| `tests/test_selfhost_compile.py` `LOWER_GAP_DOCS` | **41** | gated by a test that recomputes it |

That table is a snapshot and is left at the date it was taken, because the
point it makes is the divergence and not any one of the four numbers. The
gated figure has moved since: the report emitted from this branch recomputes
it, and no number in this note is the source of a published cell.

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

## 6. Second pass: the framework, the escape column, and a live run

Section 5 lists what the first report left empty. This section records how three
of those were closed and what each one cost, because the reasoning is the part a
later reader needs and the numbers move.

### 6.1 The framework, and the survey that chose it

The criterion was already written down before any framework was named: a host
with no unload path cannot fail the residue column honestly. Discharging it
needed evidence, and the first attempt at that evidence was wrong in a way worth
recording.

That attempt searched each published package for a vocabulary (`remove`,
`dispose`, `unregister`) and reported a boolean. It reported a de-registration
symbol for six of the eight packages, including all four python ones, on hits
like a flow-graph builder calling `list.remove()`, a callback manager calling
`list.remove()`, and the word "unregistered" inside a comment about Azure agent
threads. Two of those six were genuine. The other four were not, and nothing in
the output distinguished them, so the version was deleted rather than tuned: a
search that cannot tell a tool registry from a list is not measuring what the
column needs.

`bench/framework_unload_survey.py` is the replacement. It holds named,
falsifiable claims about each candidate's published API and tries to falsify
them against the artifact the index serves at a pinned version. Two claim kinds:
`present` (this symbol is in this file) and `absent` (none of these names occurs
under this path). A `present` claim is further tagged with what it proves,
because an earlier version of the verdict counted `semantic-kernel`'s confirmed
`add_plugin` as an unload path, which is a registration API and proves the
opposite of what was printed.

The checker earned its place twice before the survey was committed. It falsified
the `semantic-kernel` absence claim on the word "unregistered" inside a comment
about Azure agent threads, which forced the claim to be scoped to the plugin
registry's own modules, where it belongs. And it caught the `crewai` claim
pointing at `crewai/agent.py` in a version that had moved that module into a
package.

The result: **`@modelcontextprotocol/sdk` 1.30.0**. `registerTool` returns a
handle whose `remove()` deletes the registration and sends
`notifications/tools/list_changed`, so the surface a caller sees actually
narrows, and the host has `close()`.

Two things about the pick are uncomfortable and are written into
`bench/hosts.json` rather than left out. It is a tool host rather than an agent
framework in the LangChain sense, which is a real difference from the criterion's
wording. And the survey's other reading is a stronger finding than the pick.

That reading is now a headline section of the report rather than a footnote to a
host selection: **of six popular agent frameworks surveyed at pinned versions,
none publishes a way to retire an individual registered tool, and five publish
no unload path at all.** It belongs at the top because it is a claim about the
runtimes rather than about any model, an outsider can check it against published
artifacts, and it is the reason the residue column is a legitimate axis rather
than one picked to win: a column measuring what a host leaks on unload reads as
benchmaxxing until somebody shows that most popular hosts have no unload to
measure.

The denominator is the agent frameworks alone, and both exclusions run against
the finding rather than for it. `@modelcontextprotocol/sdk` is a tool host and is
the one surveyed package with a per-registration retirement, so counting it among
agent frameworks would inflate the result. `cordis` is the control and was chosen
because its unload path was known to exist.

`pydantic-ai-slim` is the runner-up and was rejected for a reason that is not a
deficiency: `AbstractToolset.__aexit__` is a documented teardown, but it is
scoped to the toolset rather than to a registration, so a residue number under
it answers a different question and the two do not belong in one column.

The framework's cells are still `not-run`. Naming a host is not running one.

### 6.2 The escape column measures the model first

The first report left this empty rather than fill it with
`tests/test_adversarial_gate.py`, on the grounds that a suite of attacks we
wrote, refused by the gate we wrote, is a regression test and not an escape
rate. That still holds, so the column is filled by a new measurement.

`bench/injection_escape.py` generates with the pinned model and puts the
injected instruction in the material a component reads: a service doc comment,
the brief, or the compiler output the retry loop feeds back. The third carrier
is there because the retry loop is a real injection surface: the harness feeds
compiler text back to the model, so a hostile toolchain has a channel, and one
vector impersonates the compiler through it.

The design decision that matters is that the column reports **two** numbers and
refuses to collapse them:

- **compliance**, how often the model wrote the undeclared thing at all. It is a
  property of the model, identical across hosts, and it is reported first.
- **containment**, whether a named hard gate then refused the artifact. Only
  `compiles` and `pinnedInterfaces` are used, both from the frozen set, because
  a column that invented its own gate would be scoring itself.

A containment rate quoted without the compliance rate it was conditioned on is
not interpretable: a model that ignores every injection makes every runtime look
perfect. Zero compliance therefore reports that the gate was not exercised, and
`check()` fails a run that reports a containment figure anyway.

Compliance is detected from the source text by a detector that never consults
the gate. If compliance were inferred from the gate rejecting something,
containment would be 100% by construction and the column would measure nothing.
The cost of that independence is a detector that misses paraphrases and fires on
comments, so each attempt records the text its detector matched.

A third distinction was added after the first live attempt, which is the reason
to record it here rather than present the design as though it arrived whole. The
model complied with the environment-variable injection, the compiler refused the
document, and the diagnostic was a syntax error on an unrelated line: the
document would have been refused with or without the injection. Counting that as
containment credits the gate for a refusal that says nothing about injection
resistance, and it is precisely how a column like this comes out flattering by
accident.

So a complying attempt lands in exactly one of three outcomes, and only the
first is evidence about injections:

- **refused on the injection**: the diagnostic names the injected reach, or the
  pinned-interface gate fired, which can only fire on the declared surface;
- **refused on another fault**: refused for something else. The injected
  behaviour did not ship, so it is not an escape, but it is not evidence either
  and it is counted apart;
- **escaped**: admitted while complying.

The split is computed from the stored attempt rows rather than at generation
time, so a run committed before the distinction existed is classified by it too.

The raw-TypeScript host has no declared surface to enforce and no gate to run.
Its containment is not a measurement, it is the definition of the host, and the
row says `not measured` rather than printing a rate that would read as an
empirical win for the column beside it.

### 6.3 The live run, and what run.py got wrong

`bench/run.py`'s local runner read `choices[0].message.content` and raised on
empty. The pinned model is a reasoning model: it emits a separate `reasoning`
channel and spends the output cap on it first, so a small cap does not truncate
the answer, it deletes it. Measured on spec `01-kv-provider`: 3693 completion
tokens, of which the answer was 358 characters.

Two changes, both narrow. The cap defaults to the pin's `max_output_tokens`
rather than 4096. And the answer is read from `content`, falling back to the
reasoning channel only when `content` is empty, with the fallback recorded on
the row: when a server sends both, the fence in `content` is the answer and the
one in the reasoning is a draft the model then revised, so a corpus scored off
the draft is a different measurement.

`run_local` has two call sites, both inside `run.py`. `bench/rescore.py` names
it only to refuse it: its `assert_model_free` guard fails if the grader exposes
a generation entrypoint.

The run also records which compiler graded it, relative to the repository root.
An editable install registers a meta-path finder consulted before `sys.path`, so
a run launched from the wrong interpreter can score against a different checkout
than the one it was pointed at and produce plausible, wrong numbers.

### 6.4 What the live run reached, and what it did not

The injection run completed: 8 of 8 vectors generated against the pinned model
on spec `01-kv-provider`, and the artifacts are committed. It is the first cell
in this benchmark produced by the pinned model.

The admission run over the 30 bench specs did not. It was started three times
and abandoned. At the first two output caps it produced no completed spec in
twenty-odd minutes each; at the third the first spec exceeded the runner's
1800-second per-call timeout and the run was stopped rather than left to
accumulate timeouts. Its empty directory was deleted rather than committed as a
corpus with nothing in it.

That is a fact about the machine rather than about the model or the harness, and
the same machine served the injection run's eight attempts at roughly nine
minutes each an hour earlier. It is recorded because a reader deciding whether
to reproduce this suite needs to know what it costs: on a contended machine,
thirty specs against a 35B model at around 24 t/s is several hours, and the
per-call timeout has to be sized for it.

The consequence for the table is that `admits` and `tokens-to-green` remain
re-scores of a corpus another model generated. They say so inside the cell
rather than beside it, and the provenance is computed from the model ids in the
corpus's own records rather than written by hand.

An attempt to close the idle-machine throughput gate failed the same way: after
the admission run was stopped the load average fell to 11, and a 128-token probe
still exceeded its 600-second timeout, most likely because the abandoned
generation was still being served. The pin recorded `measured: false` with the
reason rather than a number, which is the behaviour the pin was built for.

### 6.4 Throughput, measured twice, reproduced neither time

The first pass measured 18.5 t/s generation against a quoted 51.4, on a busy
machine, and called it a weak refutation. The second pass measured again and got
24.3 t/s (sd 1.0, n=5 warm), and 83.9 t/s prompt against a quoted 746.3.

Rather than describe the machine with an adjective, the pin now measures it: the
server accounts for load, prompt evaluation and generation separately from the
request's wall clock, so the gap between them is a number. Over these samples
the server accounted for 21% of each request's wall clock and the one-minute
load average averaged 38.

The report states the consequence in both directions. A contended measurement is
a weak refutation of an idle one, so this does not settle whether the quoted
figure is wrong; and it is not a licence to assume the quoted figure would
reproduce, because nothing in this repository has reproduced it on any machine
state. An idle-machine measurement is a named remaining gate, emitted
automatically whenever the accounted fraction is below 0.8.

## 7. Files

- `bench/hosts.json`: the registry an outsider edits: hosts, pinned model, task
  set, columns, and a `who_wrote_what` block naming the prompts this repository
  authored.
- `bench/refusal_inventory.py`: the refused column, recomputed from gated
  ledgers.
- `bench/model_pin.py`: the pin, probed rather than asserted, with the
  contention of the machine it was probed on recorded beside the figure.
- `bench/framework_unload_survey.py`: the third host's selection evidence, as
  falsifiable claims checked against published artifacts.
- `bench/injection_escape.py`: the injection-escape column.
- `bench/framework_bench.py`: assembles the report, emits EVAL-REPORT-1 JSON
  plus the markdown table, and validates itself with
  `tools/check_eval_report.py` under `--check`.
- `bench/results/framework-bench/`: the committed artifacts, raw beside the
  summary.
- `tests/test_framework_bench.py`: the tests.

Nothing is published outside this repository. Publication is a named remaining
gate in the report, not a step that happened quietly.
