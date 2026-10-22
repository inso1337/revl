# Answer ledger

What one MCP answer costs today: a deterministic, model-free baseline
for the handles and profile work (#2852, #2853). This is a measurement,
not a change: no verb and no answer shape moves here. Every number is
produced by `tools/answer_ledger.py` from the answer a client actually
receives, so it can be reviewed in a diff and re-run in CI.

## Result

| call | verb | component | answer bytes | est. tokens | echoed chars |
|---|---|---|---:|---:|---:|
| `state-idle` | `revl_state` | - | 1079 | 338 | 0 |
| `check-pulse` | `revl_check` | pulse.rvl | 2882 | 910 | 0 |
| `plan-counter-pair` | `revl_plan` | counter_pair.rvl | 3499 | 1081 | 0 |
| `audit-beacon` | `revl_audit` | beacon.rvl | 3282 | 1011 | 233 |
| `source-counter` | `revl_source` | counter_pair.rvl | 801 | 270 | 510 |
| `explain-g4` | `revl_explain` | - | 397 | 135 | 0 |
| `fmt-pulse` | `revl_fmt` | pulse.rvl | 759 | 269 | 531 |
| `refusal-ship-no-manifest` | `revl_ship` | pulse.rvl | 823 | 270 | 0 |
| **total (8 calls)** | | | **13522** | **4284** | **1274** |

Echoed source and IR is 1274 compact-serialised characters
against 13522 answer bytes (9.4%): the part of the
spend that repeats content the caller or the session already holds. The
figure is a lower bound, because the answer is indented and the echoed
values are counted without indentation. Everything else is a verdict, a
summary or a diagnostic the caller did not have before.

## Methodology

- **In process, not over a transport:** each call goes through the same
  `handle` dispatch a server uses, with a fresh session per run. No
  network, no model, no provider, no cordis boot.
- **Components:** `examples/pulse.rvl`, `examples/counter_pair.rvl` and
  `examples/beacon.rvl`, the existing examples named in the table. The
  set covers query (`revl_source`, `revl_explain`), state
  (`revl_state`), check (`revl_check`), compile-like (`revl_plan`,
  `revl_audit`, `revl_fmt`) and one refusal (`revl_ship` without the
  running manifest).
- **Answer bytes:** the length of the JSON-RPC `content[0].text` block,
  exactly the string an agent receives.
- **Estimated tokens:** `bench/tokens.py`'s `count_tokens` on that same
  text. The ledger imports the tokenizer instead of copying it.
- **Echoed characters:** a nested string value of at least 32
  characters that appears verbatim in the submitted source, plus a
  nested component IR document (a dict with `components` of named
  records and `loadOrder`). The answer root is not counted: the
  `components`/`loadOrder` summary `revl_check` carries at the top level
  is not an IR document. Values are serialised compactly, without
  indentation, so the count is a lower bound on the answer bytes they
  occupy.
- **Determinism:** fixed call order, a fresh session, the
  repeated-refusal memory cleared for the run, and the build identity
  pinned to fixed placeholders. Two runs in one process and two runs in
  separate processes produce the same table.

## Re-run

```
python3 tools/answer_ledger.py            # print the table
python3 tools/answer_ledger.py --write    # rewrite this file
python3 tools/answer_ledger.py --compare  # reproduce it; exit 1 on drift
```

`tests/test_answer_ledger_2859.py` measures a temporary fixture, checks
that two runs agree, and checks that `--compare` is green against this
file and red when a committed row is wrong.
