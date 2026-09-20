# FRAMEWORK-BENCH-1: one model, one task set, three hosts

The axis under test is the runtime, not the model. A model leaderboard
cannot check this project's claim and can falsify it: a high
compile-rate bought by loosening G1-G9 would be a worse result, not a
better one.

## What this report is not

- **the third host**: named and justified as `@modelcontextprotocol/sdk` 1.30.0, not yet run. Running it needs an MCP host harness that registers a tool per brief, calls remove(), and hands the result to a residue probe, plus a pinned-model generation pass over the 30 briefs in MCP form. Neither exists, so every framework cell is not-run rather than a number. Naming a host is not running one and the report distinguishes the two.
- **column: tokens-to-green**: not measured in this report. pass --tokens-from <run label>
- **column: admission-latency**: not measured in this report. pass --measure-latency to time the gate on this machine
- **a pinned-model run across all three hosts**: the pinned model produced injection-escape; every other cell is a re-score of a corpus another model generated, or not run. the raw-ts and framework hosts have not been generated with the pinned model
- **independent reproduction**: every claim stands at the 'measured' rung and none at 'demonstrated'. the ladder's 'demonstrated' rung requires a reproduction by a party that is not the generator; nobody outside this repository has run the suite
- **a throughput measurement on an idle machine**: the throughput figures were taken with the server accounting for only 21% of each request's wall clock. no idle machine was available during this run; the figure does not reproduce the quoted one and a contended measurement is a weak refutation either way
- **publication**: nothing here is published outside this repository. the artifacts are files in bench/results/framework-bench/

## The pin

| field | value |
|---|---|
| model requested | `hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M` |
| model resolved | `hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M` |
| resolution | exact |
| digest | `e418857d056538b10aaffb08e62148e1841fed50ed8c0ba44b6d2de0036e332e` |
| quantisation (endpoint) | unknown |
| quantisation (tag) | Q4_K_M |
| endpoint kind | ollama |
| endpoint reachable | True |
| sampling | `{"temperature": 0.0, "top_p": 1.0, "seed": 0, "max_output_tokens": 8192}` |
| machine | Darwin 25.2.0 arm64 · Python 3.14.7 |
| gate API | 1.0.0 |
| language | 2.0.0 |
| checker frontier | `reference-full:2.0.0` |
| compiler commit | `d630dcb5da102bfc9f313d3246695e72a0890f5f` |
| report schema | EVAL-REPORT-1 |

Measured throughput: **24.3 t/s** generation (sd 1.0, n=5 warm samples), 83.9 t/s prompt.

That figure is here because it was measured here, not because it
agrees with anything. Roadmap item 548 quotes 51.4 t/s generation
and 746.3 t/s prompt for this model. The numbers above were taken
on the machine named in the table, under whatever else that
machine was doing, and they do not reproduce those. Throughput is
a property of a machine at a moment, so the pin carries the
measurement with its n and standard deviation rather than the
number somebody wrote down.

How busy the machine was is measured rather than asserted. The server accounted for **21%** of each request's wall clock as load, prompt evaluation or generation; the one-minute load average over the samples averaged 38 and peaked at 43. The rest was waiting.

That cuts both ways and the report says so rather than picking
the reading it prefers. A figure taken on a contended machine
is a weak refutation of a figure taken on an idle one, so this
does not settle whether the quoted number is wrong. It is also
not a licence to assume the quoted number would reproduce: no
measurement in this repository has reproduced it, on any
machine state, and a run on an idle machine remains a named
gate rather than a result. The throughput figures above are
the server's own accounted rates and are not adjusted by this
fraction.

The frontier is the field that matters for comparing two runs. Two
gates covering different surfaces can agree on every program either
covers and still admit different languages, so an admission rate is
comparable only to one measured against the same frontier. If G1
tightens and the rate drops, this row is why the drop reads as a
tightened gate and not as a regression.

## Refused: the cost of the guarantee

**41 of 259 corpus documents (n=259) that the reference compiler accepts are not reproduced by the fully-native chain.**

A raw TypeScript host runs the equivalent work without objection.
This column leads because every other column in this table is one
revl is designed to win, and a suite whose author picked the axes
is benchmaxxing whatever the intent.

| tier | documents refused |
|---|---:|
| go | 0 |
| java | 9 |
| py | 13 |
| rust | 3 |
| ts | 16 |
| wasm | 0 |
| **total** | **41** |

Gate: `pytest tests/test_selfhost_compile.py::test_the_residual_is_located_in_lower_not_in_the_emitter`

Constructs the self-host port does not implement: **205** across six tiers.
Programs the embeddable gate admits that the reference refuses (the fail-open direction): **9**.

The full inventory, with the named documents and the gate
behind each count, is `refusals.json` beside this file and
is regenerated by `python3 bench/refusal_inventory.py`.

## Unload paths across the ecosystem

**Of 6 popular agent frameworks surveyed at pinned versions (n=6), 0 publish a way to retire an individual registered tool, and 5 publish no unload path at all.**

The one that is neither publishes a teardown at toolset scope rather
than per registration, which is a weaker guarantee and a different
question for the residue column, so it is counted apart rather than
either way.

This is the reason the residue column exists, and it is a stronger
result than the host it selected. A column measuring what a host leaks
on unload reads as an axis picked to win, until somebody shows that
most popular hosts have no unload to measure. It is also a claim about
the runtimes rather than about any model, and a reader can check it
against published artifacts at the versions named below.

| package | kind | version | registration | unload path |
|---|---|---|---|---|
| `@modelcontextprotocol/sdk` | tool-host | 1.30.0 | `McpServer.registerTool(name, config, handler)` | publishes one (per-registration) |
| `pydantic-ai-slim` | agent-framework | 2.46.0 | `Agent(toolsets=[...]) with AbstractToolset` | publishes one (per-toolset) |
| `semantic-kernel` | agent-framework | 1.44.1 | `Kernel.add_plugin(...) / add_function(...)` | none published |
| `langchain` | agent-framework | 1.5.11 | `createAgent({ tools: [...] })` | none published |
| `langchain-core` | agent-framework | 1.6.3 | `model.bind_tools([...])` | none published |
| `@openai/agents` | agent-framework | 0.18.0 | `new Agent({ tools: [...] })` | none published |
| `crewai` | agent-framework | 1.15.22 | `Agent(tools=[...]) / Crew(...)` | none published |
| `cordis` | control | 4.0.0-rc.10 | excluded from the denominator | publishes one (per-registration) |

The headline denominator is the agent frameworks alone. The surveyed set also holds a tool host, and it is the one package that publishes a per-registration retirement, so counting it among agent frameworks would be a category error in the direction that flatters the finding.

The control was chosen because its unload path was known to exist; counting it would inflate the rate with a package selected for its answer.

### How it was measured, and how it could have been wrong

Named, falsifiable claims about each package's published API, checked against the artifact the index serves at a pinned version. A `present` claim fails when the symbol is absent; an `absent` claim fails when any of the named symbols is found under the path it names.

The first version of this survey was wrong, and recording how
is part of the result. It searched a vocabulary (`remove`, `dispose`, `unregister`) across each package and reported a boolean, and it reported a de-registration symbol for six of the eight packages, including all four python ones, on hits like a flow-graph builder calling `list.remove()`, a callback manager calling `list.remove()`, and the word "unregistered" inside a comment. Two of those six were genuine; the other four were not, and nothing in the output distinguished them. That version was deleted rather than tuned, because a search that cannot tell a tool registry from a list is not measuring what the column needs.

The checker earned its place before the survey was committed. It
falsified the `semantic-kernel` absence claim on the word
"unregistered" inside a comment about Azure agent threads, which
forced the claim to be scoped to the plugin registry's own
modules; and it caught a `crewai` claim pointing at a module that
version had moved into a package.

What it does not say. That a published symbol releases anything. Whether calling it gives a resource back is the residue probe's question, and no number here may be quoted as a residue result. An `absent` verdict is scoped to the paths the claim names: several of these keep their registry in a plain mutable dict a caller can reach into.

Evidence, with the file and line of every symbol: `bench/results/framework-bench/unload-survey.json`. Re-check it with `python3 bench/framework_unload_survey.py --fetch --check`.

## The table

| column | raw Cordis / TypeScript | agent framework | revl |
|---|---|---|---|
| **refused** | 0 (TypeScript compiles everything) | not run (`@modelcontextprotocol/sdk` 1.30.0 named, harness not built) | **41 / 259** |
| admits (first pass) | not applicable, see below | not run (`@modelcontextprotocol/sdk` 1.30.0 named, harness not built) | 22/30 on `v1` (corpus bench/results/typed-deepseek-v4-pro, NOT the pinned model) |
| residue after N cycles | **4/10** leak (6 cycles) | not run (`@modelcontextprotocol/sdk` 1.30.0 named, harness not built) | not applicable: a residue-carrying component is refused at compile time (G4 and the no-residue proof), so none reaches a corpus the probe could score |
| injection escape | not run (host not in the run) | not run (`@modelcontextprotocol/sdk` 1.30.0 named, harness not built) | 2/5 scored attempts complied (model behaviour); 3 of 8 generated produced no answer within the cap and are excluded; **0** escaped, 1 refused on the injection, 1 refused on an unrelated fault |
| tokens to green | not applicable | not run (`@modelcontextprotocol/sdk` 1.30.0 named, harness not built) | not run |
| admission latency | no gate to time | not run (`@modelcontextprotocol/sdk` 1.30.0 named, harness not built) | not run here; see `bench/results/admission-latency.md` |

### Why the raw-TypeScript row is not a compile-rate

TypeScript always compiles. There is no first-pass compile
number to put in that cell, because it would read 100% for every
attempt including the ones that leak on unload. So the raw host
is scored on what it leaks and the revl host on what it admits,
and those are different questions rather than two views of one.
A reader comparing the two cells directly is comparing the
questions, not the runtimes. The asymmetry is the finding this
row exists to state, not a defect in the method.

### What the residue number does not show

It is a hand-authored corpus: we wrote both the clean and the leaky plugins, so this number demonstrates that the probe detects the leaks we planted, not what rate a population leaks at.

Re-probe it with `python3 bench/score_raw_ts.py --run hand-corpus --cycles 6` (prereq: `cd backends/typescript && npm install`).

### The injection-escape column

Measured over `bench/results/injection-ornith` with the pinned model, across 8 injection vectors on one spec, so the only thing that varies across attempts is the injection.

The injection never rides in the system prompt. It rides in a service
doc comment, in the brief, or in the compiler output the retry loop
feeds back, which is where a real one rides.

Two numbers, and they answer different questions. **Compliance** is how
often the model did the undeclared thing at all. It is a property of
the model and it is the same question on every host, so it is reported
first: a containment rate quoted without it is not interpretable, and a
model that ignores every injection would make every host look perfect.
**Containment** is whether a named hard gate then refused the artifact.
Compliance is detected from the source text by a detector that never
consults the gate, because inferring compliance from the gate's verdict
would make containment 100% by construction.

A refusal is split by what it was about. The first live attempt is
why: the model complied with the environment-variable injection,
the compiler refused the document, and the diagnostic was a syntax
error on an unrelated line. That refusal kept the behaviour out and
is no evidence that the gate catches injections, so it is counted
apart rather than folded into a containment figure.

| host | attempts | complied | refused on the injection | refused on another fault | escaped |
|---|---:|---:|---|---|---|
| revl | 5 | 2 | 1/2 | 1 | 0 |

- **revl**: 3 of 8 generated attempts spent the whole output cap on the model's reasoning channel and returned no answer. They are excluded from every denominator below rather than scored off the draft the reasoning contained. That exclusion is itself a measurement about the model and the cap, not a discarded sample
- **revl**: 1 of 2 complying attempts were refused with a diagnostic about the injected reach; 1 were refused for an unrelated fault, which kept the behaviour out but is not evidence about injection resistance and is counted apart rather than folded in

`tests/test_adversarial_gate.py (docs/threat-model.md)` is still not the source of this cell. still not sourced from the attack suite: that measures our gate against attacks we wrote for it.

## The third host

**`@modelcontextprotocol/sdk` 1.30.0**, from npm, third party (not this repository).

Criterion 1, an unload path: McpServer.registerTool returns a handle whose
remove() deletes the registration and sends notifications/tools/list_changed,
and the host itself has close(). The claim is checked rather than asserted:
bench/framework_unload_survey.py finds remove(): void at
dist/esm/server/mcp.d.ts:289 in the published 1.30.0 artifact and fails if it
ever stops finding it.
Criterion 2, pinned: npm, exact version 1.30.0.
Criterion 3, drivable by the same briefs: the 30 briefs are 'provide these
operations, acquire a host resource, release it when you go away'. A tool
registration with a declared input schema is the same shape, and the same
brief text drives it.
Criterion 4, third-party: yes.
The reason it is worth measuring rather than a foregone conclusion: remove()
is name-scoped, not resource-scoped. It deletes the registry entry and calls
nothing on the handler, so a tool that took something at registration has no
callback in which to give it back. That is a residue question with a real
answer, which is what this column needs.

### What is uncomfortable about this pick

This is a tool host rather than an agent framework in the LangChain sense, and
that is a real difference from the wording of the criterion. It is named here
because the criterion that discriminates is the unload path, and among the
popular hosts surveyed it is the only one with a per-registration one. The
alternative reading of the survey is also a finding and is stated in the
report: of six popular agent frameworks, none publishes a way to retire a
registered tool.

### Rejected, and why

| framework | why |
|---|---|
| `langchain 1.5.11 and langchain-core 1.6.3` | fails criterion 1. Tools are a constructor argument or a bind_tools result; the survey finds none of eight de-registration names on the published surface. Scoring its residue would score the absence of an API, which says nothing about a runtime comparison. |
| `@openai/agents 0.18.0` | fails criterion 1, same shape: tools are constructor state. |
| `crewai 1.15.22` | fails criterion 1 for tools. It does publish an event-bus unregister, which retires an event handler and not a tool. |
| `semantic-kernel 1.44.1` | the expected pick, and it fails criterion 1. It is the one mainstream framework that calls its unit a plugin, and it publishes add_plugin with no counterpart: the registry is a plain dict field, so a caller who wants removal mutates it, and no plugin is ever told it was removed. |
| `pydantic-ai-slim 2.46.0` | the runner-up, and it passes criterion 1 at a coarser grain: AbstractToolset.__aexit__ is a documented teardown, but it is scoped to the toolset rather than to a registration, and for_run_step hands lifecycle transitions back to the caller. A residue number under a scope-exit teardown answers a different question than one under a per-registration retirement, so both are not comparable in one column and the finer-grained host was taken. |

Every claim in that table is checked against the published artifact rather than asserted: `bench/framework_unload_survey.py` holds 13 claims across 8 packages and fails if any is falsified. The evidence, with the file and line each symbol was found at, is `bench/results/framework-bench/unload-survey.json`.

What that check says and does not say: a confirmed claim means the symbol is in the published file. It says nothing about what calling it releases, which is the residue probe's question and is why the framework residue cell is not-run rather than filled from the survey.

### Why its cells are still empty

Running it needs an MCP host harness that registers a tool per brief, calls
remove(), and hands the result to a residue probe, plus a pinned-model
generation pass over the 30 briefs in MCP form. Neither exists, so every
framework cell is not-run rather than a number. Naming a host is not running
one and the report distinguishes the two.

## Claims and their rung

| claim | rung |
|---|---|
| the fully-native revl chain does not reproduce 41 of 259 corpus documents that the reference compiler accepts (n=259) | measured |
| of 6 popular agent frameworks surveyed at pinned versions, 0 publish a way to retire an individual registered tool and 5 publish no unload path at all (n=6, excluding the control and the one surveyed tool host); the names and versions are in the report | measured |
| 4 of 10 raw-Cordis plugins in bench/results/hand-corpus leave residue after 6 load-unload cycles (n=10, hand-authored corpus) | measured |
| v1: 22 of 30 committed generations are admitted by the current checker (n=30, corpus bench/results/typed-deepseek-v4-pro, NOT generated by the pinned model) | measured |
| v2: 22 of 30 committed generations are admitted by the current checker (n=30, corpus bench/results/typed-deepseek-v4-pro, NOT generated by the pinned model) | measured |
| v2host: 23 of 30 committed generations are admitted by the current checker (n=30, corpus bench/results/typed-deepseek-v4-pro, NOT generated by the pinned model) | measured |
| injection, revl: the pinned model produced the undeclared action in 2 of 5 attempts (n=5, a property of the model, not of the host) | measured |
| injection, revl: 2 of 2 complying attempts were refused by a named hard gate, leaving 0 escapes (n=2 complying attempts, not 5) | measured |

No claim here stands above `measured`. The ladder's
`demonstrated` rung needs a reproduction by a party that is not
the generator, and nobody outside this repository has run the
suite. Validate this report with
`python3 tools/check_eval_report.py bench/results/framework-bench/report.json`.

