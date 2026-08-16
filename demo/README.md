# The live hot-swap demo

Edit a `.rvl` file; watch the component it declares get recompiled and
swapped into a system that never stopped running — dependents deactivating
and reactivating around it, every effect it acquired replayed backwards, and
nothing left behind.

This is the live successor to [`backends/python/demo.py`](../backends/python/demo.py).
That one replays a checked-in IR document through a scripted sequence; this
one drives the whole pipeline from source on every change:

```
demo/components/*.rvl  --revl frontend-->  IR  --cordis-py backend-->  module
                                                                          |
                              one running cordis.Context  <---------------+
```

## Run it

The demo needs *both* halves of the project on one interpreter: the compiler
(pure stdlib, `src/`) and the cordis-py runtime the emitted components run
on. The backend venv has the runtime, so use that interpreter — `live.py`
puts `src/` and `backends/python/` on `sys.path` itself. If you have not
built it yet, run [`backends/python/setup.sh`](../backends/python/setup.sh)
first.

```sh
# interactive: loads the composition, then watches demo/components/*.rvl
backends/python/.venv/bin/python demo/live.py

# scripted: the same sequence, non-interactive, exits nonzero on any failed check
backends/python/.venv/bin/python demo/live.py --script
```

`--script` is the CI mode: it performs the edits itself (writing files from
`demo/variants/` over `demo/components/pg_database.rvl` and restoring the
original on the way out) and asserts nineteen properties about what happened.
Set `NO_COLOR=1` for plain output.

### What to do in interactive mode

| Do this | Watch for |
|---|---|
| Paste `variants/pg_database.hotswap.rvl` over `components/pg_database.rvl` | the full cascade — `UserCache` tears down *before* the pool closes, then reactivates against the new provider |
| Delete the `undo pool.close()` clause | a `reject` line: the swap never reaches the runtime, and traffic keeps flowing on the old version |
| Change the SQL string in `user_cache.rvl` | the consumer alone swaps; the provider and its pool are untouched |
| Drop a new `.rvl` file into `components/` | a component joins the running system; if its requirement is unmet it simply waits in `PENDING` |
| Delete that file again | it unloads, and its inverses run |
| Ctrl-C | teardown, then the no-residue checks |

## Reading the log

Every line is `elapsed | seq | channel | subject | detail`.

| channel | what it is |
|---|---|
| `compile` | the revl frontend: parse → check → link. The `link ok` line prints the provision map the linker just proved disjoint (G2) and acyclic (G3). |
| `reject` | a guarantee refused the edit. Nothing was loaded. |
| `emit` | the cordis-py backend turning IR into a module (`genN` = swap generation). |
| `load` | the host admitting a component, with its signature: what it requires, what it provides. |
| `fiber` | the runtime's lifecycle state machine — `PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED`, the paper's Figure 2. |
| `host` | operations on host resources (`pool#1`, `map#2`). **This is the residue channel**: everything opened here must be closed by the end. |
| `call` | ordinary traffic through a provided service. |
| `swap` | swap orchestration by the host. |
| `check` | an assertion, PASS or FAIL. |

The four moments worth pausing on:

1. **Cold start.** `UserCache` is loaded *first*, on purpose, and sits in
   `PENDING` — nobody provides `db` yet. When `PgDatabase` activates,
   `UserCache` activates by itself. Nothing in either component's source
   mentions the other; the `db` key is the whole interface.

2. **The rejected edit.** An acquisition without an inverse fails to compile
   with the file and line. The running composition never learns about it.
   The unit of deployment is a *checked* component.

3. **The swap.** Disposing the old `PgDatabase` fiber sends `UserCache`
   `ACTIVE → UNLOADING` before the provider finishes: the cache's own
   inverses (`map.remove bob`, `map.remove alice`, `map.drop`) all run
   *before* `pool.close`. Nobody wrote that ordering; the runtime derives it
   from the dependency edge. Then the new provider loads and `UserCache`
   goes `PENDING → LOADING → ACTIVE` against it, with a fresh store — the
   old one was reverted, not reused. The `pool.query SELECT
   pg_advisory_lock(42)` line is behavior that exists only in the *edited
   source file*, now running in the process that started three seconds ago.

4. **Teardown.** The last four host operations are the whole point:

   ```
   map.remove alice                        <- an effect installed by cache.put(), while ACTIVE
   map.drop                                <- the consumer's activation-time effect
   pool.query SELECT pg_advisory_unlock(42) <- the provider's second acquisition
   pool.close postgres://primary:5432/app   <- its first
   ```

   Newest first, across two components, mixing activation-time and
   method-time effects — and the provider's inverses run only after every
   dependent has drained. Then `registry`, `provisions`, `effects`,
   `listeners` and `host-resources` confirm the Context is byte-for-byte
   back where it started.

## Layout

| path | role |
|---|---|
| `live.py` | the demo: compile → emit → load → traffic → watch → swap → teardown |
| `components/services.rvl` | the service (coeffect) vocabulary shared by every component |
| `components/pg_database.rvl` | the provider: owns a pool, provides `db` |
| `components/user_cache.rvl` | the consumer: requires `db`, provides `cache` |
| `variants/pg_database.hotswap.rvl` | the good edit `--script` applies (extra acquisition, bigger pool) |
| `variants/pg_database.rejected.rvl` | the bad edit `--script` applies (acquisition with no inverse, G4) |

### Why services live in their own file

The linker rejects two providers of one key inside a single composition
(G2) — which is exactly right, and exactly what makes "compile the old and
new version together" impossible. So a swap compiles the changed file **as
its own composition**: `compile_files([services.rvl, changed.rvl])`. Service
declarations therefore have to be shareable, which is why they sit in a file
of their own rather than being repeated per component (`compile_files`
rejects a service declared twice). Editing `services.rvl` recompiles every
component, because the vocabulary itself moved.

## What this demo proves

The paper's plug-in metaphor, live, from compiled source — with the parts a
library cannot promise now promised by a compiler:

- **Temporal composability.** Every mutation in these components sits in an
  `effect … undo …` form, so the compiler can *derive* teardown. The demo
  never writes an unload path, yet three different unloads (a swap, a
  reactive deactivation, a shutdown) each replay exactly the right inverses
  in exactly LIFO order — including effects installed by `cache.put` long
  after activation. `map.drop`/`pool.close` accounting at the end shows the
  environment is recovered, not approximately cleaned.

- **Spatial composability.** `UserCache` names `db: Database` and nothing
  else. It waits for a provider, follows it when it leaves, and binds to a
  replacement — a *different compilation* of a different source file — with
  no coordination code on either side. Adding or deleting a file in
  `components/` adds or removes a live participant.

- **The checked part.** The rejected edit is the demo's real argument.
  Hot-swapping is only interesting if the thing you swap in cannot corrupt
  the system, and here the compiler is the admission gate: an acquisition
  with no inverse (G4), an undeclared access (G1), a provision conflict
  (G2), a cycle (G3) or an acquisition below a `provide` (A2) never becomes
  a module at all. Same checker, same messages, whether the component is
  linked at build time or admitted into a running process — which is what a
  self-deploying agent harness needs (DESIGN.md §1).

The runtime semantics being exercised are the backend contract's R1–R5
(LIFO recovery, reactive resolution, withdrawal ordering, no residue,
derived provision withdrawal); `backends/python/tests/` asserts them in
isolation, this demo shows them happening to a composition you can edit.
