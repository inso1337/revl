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
4. Open a PR that closes the issue:

   ```
   gh pr create --repo inso1337/revl --fill --body "Closes #<issue>"
   ```

5. Report what you changed, what you ran, and the PR number. Then stop.
   **Do not wait for CI.** The orchestrator watches it.

## What an agent does not do

- Do not run the full suite locally. That is CI's job now.
- Do not block waiting on a background suite. Report and stop.
- Do not fix a red you did not cause. A pre-existing failing test or a red CI on
  main belongs to the orchestrator. Say you saw it and leave it alone.
- Do not merge your own branch.

## What CI covers

`lint`, `frontend`, `frontend-cordis`, `backend-python`, `backend-typescript`,
`backend-wasm`, `backend-rust`, `backend-java`, `backend-go`, `conformance`,
`formal`. `pull_request` carries no branch filter, so every PR gets all of it.

## What CI does not cover

Anything gated on an env switch that no job sets, and anything needing a
toolchain the runner lacks. `tests/test_env_gated_skips_run_somewhere.py` fails
when a test reads an env name that nothing sets, which is what keeps this list
from growing silently. If your change needs a switch flipped, flip it in
`ci.yml` in the same PR.

## Merging

The orchestrator merges when CI is green, and regenerates the site wheel and the
gate crate if the change touched them. A branch is never merged on a local
green alone, because a local green certifies only what actually ran on that
machine at that moment.

## Issues are the state

Every unfinished roadmap item has an issue. The roadmap stays the
reasoning-of-record: evidence, cross-references, negative results. The issue
carries the state, so nobody starts the same work twice. Before you begin,
check the issue is not already assigned or already closed.

Security findings do not get public issues. They go to private security
advisories.

## Goldens

Six backends carry checked-in emitter output, and so does `crates/revl-gate`.
They are **snapshot tests, not a freeze**: the invariant is "emitter output
never changes unreviewed", never "output never changes". Regenerating a golden
and reviewing its diff is always an acceptable resolution. Bending an emitter
back to keep old bytes is not.

One command covers every tier:

```
python3 tools/regen_goldens.py             # list every target and the files it owns
python3 tools/regen_goldens.py --check     # which goldens drifted, and the fix for each
python3 tools/regen_goldens.py <target>    # regenerate: python typescript rust java wasm go gate-crate
```

Rules:

1. If you change an emitter, regenerate its goldens **in the same commit** and
   review the diff. A separate follow-up commit means main is red in between.
2. `crates/revl-gate` embeds emitted rust. Any change to `backends/rust/emit.py`
   or to `selfhost/*.rvl` rewrites about 1900 lines of it, and a PR that skips
   the regeneration goes red on drift alone. `python3 tools/regen_goldens.py
   gate-crate` fixes it.
3. A red golden test names the exact command that resolves it. Run that command,
   read the diff, and keep it only if the new bytes are what you meant.
4. Adding a golden means adding it to `tools/regen_goldens.py`. A generated file
   nothing can reproduce is a file nothing can check.

The policy and its two declared exceptions live in
[conformance.md](conformance.md), "Golden policy: snapshot, not freeze".
