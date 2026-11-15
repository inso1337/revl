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

It also records a `body_sha256` pair. That hash is over the file with the
module docstring token removed; the tokenizer is used rather than an AST dump
because an AST dump's text changes between Python versions. The pair records
the source body and the tracked body, and the two differ because every tracked
driver carries the encoding edit below.

Each moved file carries a few mechanical edits, listed per driver in `edit`:
three capped variants had their docstring identity and usage lines corrected;
all six name explicit UTF-8 encodings on text-mode reads, writes and open
calls; the default `REVL_REPO` is now derived from the driver's own location
instead of a machine path (and gen3's `--arm-src` is required instead of
defaulting to a `/tmp` path); and a long dash in a comment was replaced with
plain punctuation. None of these changes what a driver sends to a model. On
macOS and Linux the platform default encoding was already UTF-8, and on Windows
it now matches. The repository's encoding guards (issues #2639 and #2648)
reject the platform default anywhere in the tree. Nothing else was rewritten;
the source files are untracked and not in this repository, so a reader can
check the tracked hash here, and the source hash only against a copy of the
original.

Two long dashes per driver (none in gen1) were left on purpose, because
changing them is not a punctuation fix: one sits inside the `TASK_AUTHOR`
prompt string sent to the model (the clause after `ON DISK`, before `check the
file itself`), where an edit would change the benchmark, and one is the
placeholder `_agg` returns for an empty column of the printed summary table.

### What is known of each source

`source` names where the file was found; the manifest adds `source_reread` and
`source_record` per entry. A source that could be read again when this record
was reviewed (2026-11-12) has its `source_sha256` recomputed and matching, and
its size in bytes and modification time recorded in `source_record`: that is
`bench_agentic2_cap2.py` (the daily bench directory of the lane workspace) and
`bench_agentic3.py` (the bench-agentic directory of the lane workspace). Four
sources could not be re-read for this review and have no size or modification
time on record: `bench_agentic.py`, `bench_agentic2.py`, `bench_agentic2_cap.py`
and `bench_agentic2_cap6.py`, all from the `bench10_out` scratch directory. For
those four `source_sha256` is a value recorded at copying time and was not
verified; it is a record, not a check. The size and modification time of the
two that were re-read are as read at review, not as at copying.

`tests/test_bench_agentic_drivers.py` enforces the record: the six-variant
inventory, the tracked hashes, the docstring identity, and the tracked body
hashes, which are pinned in the test and not only in the manifest. A body edit
therefore fails even if the manifest is refreshed with it, and a future copy
that keeps another driver's docstring fails too. `generation`, `predecessor`
and `tool_result_cap` are records of the code, not machine-checked values; the
test only checks that every predecessor names a tracked variant.

## Published numbers

The arm tables in the agentic development spec are the published numbers. They
name a variant and a number, but not yet a driver hash; that is the remaining
half of issue #2621. Until the spec carries it, cite the driver name and its
`tracked_sha256` from `manifest.json` beside any number taken from these
drivers, for a run made after this directory existed. A number published before
it existed cannot be bound to a hash, only to a driver name: nobody can
establish that a scratch file was unchanged between a published run and the
copy, so `source_sha256` records the copy and does not vouch for the run.

The scratch copies were left where they were found; they are untracked and not
part of this repository, so the manifest's `source_sha256` is the record of
them, not a path a reader can fetch. They can be dropped once the spec records
the tracked revision.

## Running

The gen1 and gen2 drivers expect a local oMLX server; the gen3 driver expects
any OpenAI-compatible endpoint and reads its key only from the environment.
Run them with an interpreter that can import `mcp`: the driver talks to the
MCP server in-process and compiles with `sys.executable -m revl`, with
the repository's `src` (`REVL_REPO`, by default the repository containing this
directory) put on that interpreter's `PYTHONPATH`. `REVL_MCP_PYTHON` names
the separate interpreter that serves the MCP server (`cordis-py`), and
`REVL_EXTRA_PATH` adds import directories for it.

    OMLX_URL=http://127.0.0.1:8010/v1/chat/completions OMLX_MODEL=<model> \
    REVL_REPO=/path/to/revl REVL_MCP_PYTHON=/path/to/python \
    python3 bench/agentic/gen2/bench_agentic2_cap2.py --exp scaffold

    BENCH_BASE_URL=<url> BENCH_API_KEY=<key> BENCH_MODEL=<model> \
    python3 bench/agentic/gen3/bench_agentic3.py --exp both --reps 1

They were moved as they were found, apart from the edits above: they write
workdirs and result rows beside themselves, and they assume the fixtures and
trees the spec names. `REVL_REPO` defaults to the repository these files sit
in; gen3's `--arm-src FILE` (the file holding the arm system prompt) must be
given with `--workdir`. The scrub lists name environment variables, including
`KIRA_API_KEY`, and carry no value. A run drives a real model, so check the
endpoint and budget first; nothing here is run by CI.

## Edits to a published driver

A preserved driver may be edited only in its module docstring, its usage
lines, or an argument a repository guard requires, which is what the identity
and encoding fixes are. A behaviour change makes it a new variant under the
generation that introduces it, with a new name and a new manifest entry; a
tracked driver is not edited in place. The tracked body hashes pinned in the
test are what makes that rule enforceable: a change outside those edits fails
even when the manifest is refreshed with it, because changing the pin is a
visible edit to the test.
