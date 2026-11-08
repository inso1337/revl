# AgentDojo: baseline vs admitted revl

Issue #2683 asks for one public agent-security suite, run with two arms over the
same task set, models and seeds:

- **baseline** — AgentDojo's own reference pipeline: the model calls the suite's
  tools directly, through the suite's `FunctionsRuntime`.
- **revl** — every suite tool is a declared revl service. For each tool call the
  model writes a small revl component, which is admitted under
  `AdmissionProfile.untrusted_author(granted=<the suite's tools>)` and then run on
  a live session. The admitted component's calls are the suite's own tools
  against the suite's own environment, so AgentDojo's task checks score both
  arms on the same facts.

Both arms are driven through `TaskSuite.run_task_with_pipeline`, so utility and
security (attack success) are AgentDojo's own task code, not ours.

## Licence and citation

AgentDojo is MIT licensed (Copyright 2024 Edoardo Debenedetti, Jie Zhang, Mislav
Balunović, Luca Beurer-Kellner, Marc Fischer, Florian Tramèr). This driver pins
`agentdojo==0.1.35` and does not vendor its data. Cite:

> Edoardo Debenedetti, Jie Zhang, Mislav Balunović, Luca Beurer-Kellner, Marc
> Fischer, and Florian Tramèr. *AgentDojo: A Dynamic Environment to Evaluate
> Attacks and Defenses for LLM Agents.* NeurIPS 2024 Datasets and Benchmarks.

## Setup

The driver needs one interpreter that can import AgentDojo, cordis and the revl
checkout under test:

```sh
sh backends/python/setup.sh          # creates backends/python/.venv with cordis
backends/python/.venv/bin/pip install 'agentdojo==0.1.35'
backends/python/.venv/bin/pip install -e .   # the revl under test
```

The model endpoint must be OpenAI-compatible. The recorded runs use a loopback
ollama endpoint; the model digest is read from `/api/tags` and written to
`meta.json` beside the records. A model name without a digest is not a pin.

## Run

```sh
backends/python/.venv/bin/python bench/agentdojo/run.py \
    --suite banking \
    --arms baseline,revl \
    --models flint-bench:latest,ornith-revl:latest \
    --runs 2 \
    --tasks user_task_0,user_task_1 \
    --injection-tasks injection_task_0,injection_task_1 \
    --label pilot-banking
```

Records are appended to `bench/results/agentdojo/<label>/records.jsonl` (one
row per task, condition and run) and `meta.json` records the suite, tasks,
models, digests, temperature and command. `--resume` skips completed rows, so an
interrupted run continues rather than restarting.

Summarise:

```sh
backends/python/.venv/bin/python bench/agentdojo/report.py --label pilot-banking
```

## What the record holds

- `utility`: AgentDojo's user-task check on the environment the arm produced.
- `security`: AgentDojo's injection-task check; `true` means the injection
  *succeeded* (attack success). It is `null` for the benign condition.
- `stats.admission_refusals` and `stats.refusal_codes`: revl-arm only. A refusal
  is reported, not scored as a success: the component never ran.
- `stats.steps`, `stats.calls`, `stats.prompt_tokens`, `stats.completion_tokens`
  and `duration_s`: the cost side.
- `error`: a driver or provider failure for that row; such a row is not a task
  result and the report lists it separately.

## The limit that is stated up front

G4 bounds which emissions an admitted component can reach. It does not stop
data-level injection through an effect the task legitimately grants: an injected
recipient passed to an allowed `send_money` is admitted and runs. Those are
expected attack successes in the revl arm, and the report classifies them; they
are not hidden or counted as refusals.

The revl arm is also not a claim that a model writes revl as well as it writes
native tool calls. Utility, repair turns and tokens are measured, not assumed.
