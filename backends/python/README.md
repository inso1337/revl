# revl cordis-py backend

The Python backend for revl's backend IR (`docs/backend-ir.md`): an emitter
that lowers an IR document to one cordis-py plugin module, the runtime
adapter + host support the emitted code imports, and a demo/test suite that
proves the contract's required semantics R1–R5 against the real runtime.

This is the REFERENCE tier: the other backends target the behaviour this one
executes, and it has grown well past its original v0 core. Beyond the R1–R5
lifecycle it now carries witnessed/transactional teardown with `emit …
compensate …` (items 243/245/247), a write-ahead log with `revl recover`
crash-recovery and backwards replay (`replay.py`, `docs/replay.md`), declared
`Secret[T]` confidentiality that redacts at capture (`confidential.py`, item
256), a py<->py interop bridge (`bridge.py`), and workspace-confined witnessed
`fs`/`shell` host support (`revl_fs_workspace.py` / `revl_shell_*.py`, items
244/252).

## Layout

| file | role |
|---|---|
| `emit.py` | `emit(ir: dict) -> str` — IR document → Python module source |
| `runtime.py` | adapter (`Frame`, `ConfigSchema`, `fmt`) + host builtins (`Pool`, `Map`); the per-activation LIFO accumulator and witnessed/transactional teardown, incl. `emit … compensate …` (items 243/245/247) |
| `loader.py` | the single place that execs an emitted IR as an importable module (`load(ir)`), shared by `demo.py`, the tests, and `demo/live.py` |
| `confidential.py` | the `Secret[T]` confidentiality choke point (item 256): redacts declared-secret values at capture (WAL record, timeline, config echo) so no downstream printer can leak them |
| `replay.py` | backwards replay over the effect accumulator, the `WriteAheadLog`, the `Timeline`, and the `revl recover` crash-recovery reader (`docs/replay.md`) |
| `bridge.py` | py<->py interop bridge (`docs/interop-bridge.md` §3): a service provided in one process, consumed in another over an AF_UNIX socket, with peer-death withdrawal and seam deadlines |
| `revl_fs_workspace.py` | session workspace-root confinement for the witnessed `stdlib/fs.rvl` externs (item 244); imported by the `@py` fs bodies |
| `revl_shell_classify.py` | pure, total shell-command classifier (item 252): lowers a provably-fs-local command onto the witnessed catalog, else one honest `emission` |
| `revl_shell_host.py` | py-tier host bodies for `stdlib/shell.rvl` (item 252): the opaque `sh -c` fallback plus the re-exported classifier |
| `golden/user_cache.py` | emitter output for `examples/user_cache.ir.json`, checked in |
| `demo.py` | load → put/get → hot-swap the Database provider → unload, with event log |
| `tests/` | pytest suite; each test names the R1–R5 requirement it covers |
| `tests/user_cache.ir.json` | byte-identical vendored copy of the reference IR |
| `REPORT.md` | impedance mismatches, IR contract gaps, LOC, ship recommendation (a frozen early artifact; its LOC counts are not kept current) |

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
driver (roadmap §2; `docs/guide-humans.md` Tooling). `setup.sh` installs the
`revl` console script into this venv's `bin/`, so the documented happy path
is `revl run …` (no `python -m`; issue #317 closes the CWD-shadowing window
that the `-m` form still has):

```sh
revl run ../../examples/user_cache.rvl --config cfg.toml
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
