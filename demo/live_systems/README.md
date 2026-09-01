# The live-systems demo (v3.0 gate E3)

revl's cross-tier live migration, run end to end from a clean checkout as one
scripted, repeatable command:

    sh backends/python/setup.sh                                   # once: the cordis-py runtime
    backends/python/.venv/bin/python demo/live_systems/run_demo.py

or `make demo`. It drives the real `revl` CLI through three stages and asserts
each one's observable outcome:

1. **`revl swap`** ([docs/swap.md](../../docs/swap.md)) splits `app.rvl` across
   two py processes over a Unix-socket seam, then live-migrates the provider
   `MemCache` into a fresh process (`swap MemCache --to py`). Asserts the
   successor booted, the consumer re-pointed onto the new socket, and the old
   provider drained with a no-residue proof.
2. **`revl why`** ([docs/why-runtime.md](../../docs/why-runtime.md)) records the
   causal trace of a withdrawal and explains it: the cause chain names the
   migration, and the prediction-vs-actuality oracle reports the runtime tore
   the composition down in exactly the compiler's predicted set and LIFO order
   (`CONFORMS`).
3. **`revl plan` / `revl apply`** ([docs/apply.md](../../docs/apply.md)) turns a
   plan into an executable artifact, lands it, then forces a mid-plan failure
   and asserts the applied prefix rolls back by derived LIFO inverses with no
   residue.

All artifacts go to a throwaway temp dir; the demo leaves the checkout
untouched and passes on a second run as cleanly as the first.

| file | role |
|---|---|
| `app.rvl` | the running composition: `MemCache` provides `cache` (async, transport-safe), `Api` consumes it |
| `split.toml` | places `MemCache` and `Api` in separate py processes, wiring the `cache` seam |
| `candidate.rvl` | the plan/apply candidate: `app.rvl` plus a new `Audit` consumer |
| `run_demo.py` | the scripted runner and its assertions |

`pytest tests/test_e3_demo.py` runs this wherever the cordis-py runtime is
present and skips loudly where it is not; CI runs it in the conformance job.
