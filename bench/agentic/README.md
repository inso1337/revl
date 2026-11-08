# The agentic benchmark drivers

The drivers behind the agentic arm comparisons (issue #2621), under version
control: can a model drive revl through its MCP surface, and how does the revl
loop compare with file editing and with TypeScript?

Before this directory existed none of the six was tracked. Four lived in a
scratch directory, two in a working lane, and three of the capped variants
opened with `bench_agentic2.py`'s docstring, so the filename was the only
identity they had. `manifest.json` records where each was found and its
sha256, and `tests/test_bench_agentic_drivers.py` fails if a copy drifts back
to another variant's identity.

## Generations

One directory per experiment generation, with the variants of a generation
beside each other.

| driver | forked from | what it measures | tool-result cap |
| --- | --- | --- | --- |
| `gen1/bench_agentic.py` | none (R10) | the change task: withdraw a component and state the exact break set, across revl MCP, file-only and TypeScript arms | 6000 |
| `gen2/bench_agentic2.py` | `bench_agentic.py` | the two assigned experiments: scaffold-first vs free generation, and preflight-mandatory vs free order | 6000 |
| `gen2/bench_agentic2_cap.py` | `bench_agentic2.py` | the same two experiments with the cap raised so the surface's export note is not cut, and `revl_verbs` `names` hatches counted | 24000 |
| `gen2/bench_agentic2_cap6.py` | `bench_agentic2.py` | the same two experiments with truncation accounting: `finish_reason`, `reasoning_only` turns, `truncated_turns`, and `REVL_MAX_TOKENS` | 6000 |
| `gen2/bench_agentic2_cap2.py` | `bench_agentic2_cap.py` | the same two experiments with `BENCH_CAP`, `out_len`/`cut` per call, `export_calls`, `write_file_calls` and `tool_cap` recorded; the daily usability driver | `BENCH_CAP` (default 24000) |
| `gen3/bench_agentic3.py` | `bench_agentic2_cap2.py` | the same experiments against any OpenAI-compatible endpoint: wall timeout, 429/5xx retry, per-turn latency, scrubbed key, prompt sha | `BENCH_CAP` (default 24000) |

## Provenance

`manifest.json` is the record. For each driver it gives the untracked source it
was recovered from, the sha256 of the file as found (`source_sha256`), the
sha256 of the tracked copy (`tracked_sha256`), the tool-result cap, and the
variant it was forked from.

It also records a `body_sha256` pair. That hash is over the source with the
module docstring token removed, so `source_body_sha256 == tracked_body_sha256`
proves that the only edit made to a moved file is its docstring. The tokenizer
is used rather than an AST dump because an AST dump's text changes between
Python versions.

Three variants were edited: their docstrings named `bench_agentic2.py` and now
name themselves, and their usage lines follow. For the other three, `edit:
none` and a tracked hash equal to the source hash record an unchanged copy;
`cmp` confirmed that when the copies were made. The source files are not in
this repository, so a reader can check the tracked hash here, but the source
hash only against a copy of the original.

`tests/test_bench_agentic_drivers.py` enforces the record: the six-variant
inventory, the tracked hashes, the docstring identity, and the body hashes,
which are pinned in the test and not only in the manifest. A body edit
therefore fails even if the manifest is refreshed with it, and a future copy
that keeps another driver's docstring fails too. `generation`, `predecessor`
and `tool_result_cap` are records of the code, not machine-checked values; the
test only checks that every predecessor names a tracked variant.

## Published numbers

The arm tables in the agentic development spec are the published numbers. They
name a variant and a number, but not yet a driver hash; that is the remaining
half of issue #2621. Until the spec carries it, cite the driver name and its
`tracked_sha256` from `manifest.json` beside any number taken from these
drivers, or `source_sha256` for a run made before this directory existed, if
the file has not changed since. The scratch copies were left where they were
found; they are untracked and not part of this repository, so the manifest's
`source_sha256` is the record of them, not a path a reader can fetch. They can
be dropped once the spec records the tracked revision.

## Running

The gen1 and gen2 drivers expect a local oMLX server; the gen3 driver expects
any OpenAI-compatible endpoint and reads its key only from the environment.
Run them with an interpreter that can import `mcp`: the driver talks to the
MCP server in-process and compiles with `sys.executable -m revl`, with
`REVL_REPO/src` put on that interpreter's `PYTHONPATH`. `REVL_MCP_PYTHON` names
the separate interpreter that serves the MCP server (`cordis-py`), and
`REVL_EXTRA_PATH` adds import directories for it.

    OMLX_URL=http://127.0.0.1:8010/v1/chat/completions OMLX_MODEL=<model> \
    REVL_REPO=/path/to/revl REVL_MCP_PYTHON=/path/to/python \
    python3 bench/agentic/gen2/bench_agentic2_cap2.py --exp scaffold

    BENCH_BASE_URL=<url> BENCH_API_KEY=<key> BENCH_MODEL=<model> \
    python3 bench/agentic/gen3/bench_agentic3.py --exp both --reps 1

They were moved as they were found, not rewritten: they write workdirs and
result rows beside themselves, and they assume the fixtures and trees the spec
names. The preserved defaults are the author's trees (`REVL_REPO` defaults to
`/Users/inso/Projects/revl`, gen3's `--arm-src` to a `/tmp` path from an older
run), so override them on another machine. The scrub lists name environment
variables, including `KIRA_API_KEY`, and carry no value. A run drives a real
model, so check the endpoint and budget first; nothing here is run by CI.

## Edits to a published driver

A tracked driver may be edited only in its module docstring while it is a
preserved artifact, which is what the three identity fixes did. A behaviour
change makes it a new variant under the generation that introduces it, with a
new name and a new manifest entry; a published driver is not edited in place.
The body hashes pinned in the test are what makes that rule enforceable: a
change outside the docstring fails even when the manifest is refreshed with it,
because changing the pin is a visible edit to the test.
