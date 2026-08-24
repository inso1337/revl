# revl cordis-py backend (v0)

The Python backend for revl's backend IR (`docs/backend-ir.md`): an emitter
that lowers an IR document to one cordis-py plugin module, the runtime
adapter + stub stdlib the emitted code imports, and a demo/test suite that
proves the contract's required semantics R1–R5 against the real runtime.

## Layout

| file | role |
|---|---|
| `emit.py` | `emit(ir: dict) -> str` — IR document → Python module source |
| `runtime.py` | adapter (`Frame`, `ConfigSchema`, `fmt`) + host builtins (`Pool`, `Map`) |
| `golden/user_cache.py` | emitter output for `examples/user_cache.ir.json`, checked in |
| `demo.py` | load → put/get → hot-swap the Database provider → unload, with event log |
| `tests/` | pytest suite; each test names the R1–R5 requirement it covers |
| `tests/user_cache.ir.json` | byte-identical vendored copy of the reference IR |
| `REPORT.md` | impedance mismatches, IR contract gaps, LOC, ship recommendation |

## Setup (one command)

Requires `uv` and network access for the runtime clone:

```sh
./setup.sh
```

This clones [cordis-py](https://github.com/inso1337/cordis-py) at branch
`harden-fiber-lifecycle` (the lifecycle-reentrancy-hardened fork the
semantics depend on; upstream PR geohotstan/cordis-py#1) into `.cordis-py/`,
**pinned to the tested commit `1316174`** (docs/contract-errata.md A8) —
never the branch's moving HEAD, which made vintage-pinned tests fail on fresh
worktrees through no fault of the change under test (findings-faultres).
Update the pin in `setup.sh` deliberately when the runtime moves, and record
why. Point `CORDIS_PY=/path/to/clone` at an existing checkout to skip the
clone; override the pin with `CORDIS_PY_REV=<sha>`. The script creates
`.venv/` and installs everything.

## Test (one command)

```sh
.venv/bin/pytest
```

## Demo

```sh
.venv/bin/python demo.py
```

Prints a numbered event log showing R2 (UserCache deactivates when the
primary PgDatabase is unloaded, reactivates against the replica instance)
and R3 (the dependent's inverses — `map.remove`, `map.drop` — all run before
the provider's `pool.close`), then asserts R1 (LIFO teardown tail) and R4
(no hooks, provisions, runtimes, or effects left on the Context).

## Run a composition from the CLI (`revl run`)

The same emitter + runtime, driven by the revl CLI with no host-language
driver (roadmap §2; `docs/guide-humans.md` Tooling):

```sh
.venv/bin/python -m revl run ../../examples/user_cache.rvl --config cfg.toml
```

`revl run` compiles the manifest, boots the composition on a cordis `Context`,
streams the lifecycle/host trace (`load` / `fiber` / `host` lines), holds a
REPL over the provided services, and on EOF/Ctrl-C tears everything down and
proves no residue. `--watch` hot-swaps edits in; `--plan` prints the load plan
with no runtime. A component admitted with a missing *required* config field
is refused before the runtime loads.

## Regenerating the golden file

```sh
.venv/bin/python emit.py ../../examples/user_cache.ir.json > golden/user_cache.py
```

`tests/test_emitter.py::test_golden_file_regenerates_identically` diffs the
checked-in file against a fresh `emit()` on every run.
