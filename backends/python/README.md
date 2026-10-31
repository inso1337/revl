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
| `revl_net.py` | the stdlib-only HTTPS engine imported by the `stdlib/net.rvl` `@py` bodies: JSON and urlencoded bodies, custom headers, HTTPS only, timeout, bounded response, no redirect following, errors as data |
| `golden/user_cache.py` | emitter output for `examples/user_cache.ir.json`, checked in |
| `demo.py` | load → put/get → hot-swap the Database provider → unload, with event log |
| `tests/` | pytest suite; each test names the R1–R5 requirement it covers |
| `tests/user_cache.ir.json` | byte-identical vendored copy of the reference IR |
| `REPORT.md` | impedance mismatches, IR contract gaps, LOC, ship recommendation (a frozen early artifact; its LOC counts are not kept current) |

## Setup (one command)

Requires `git`, `uv` and network access for the runtime clone. Without them,
see "Obtaining the runtime without the clone" below:

```sh
./setup.sh
```

This clones [cordis-py](https://github.com/inso1337/cordis-py) at branch
`harden-fiber-lifecycle` (the lifecycle-reentrancy-hardened fork the
semantics depend on; upstream PR geohotstan/cordis-py#1) into `.cordis-py/`,
**pinned to the tested commit `1c5e6f17`** (the dict-plugin Config fix on top
of the `1316174` A8 async-body fix, docs/contract-errata.md A8) —
never the branch's moving HEAD, which made vintage-pinned tests fail on fresh
worktrees through no fault of the change under test (findings-faultres).
Update the pin in `setup.sh` deliberately when the runtime moves, and record
why. Point `CORDIS_PY=/path/to/clone` at an existing checkout to skip the
clone; override the pin with `CORDIS_PY_PIN=<sha>`. The script creates
`.venv/` and installs everything.

## Obtaining the runtime without the clone

`setup.sh` is the path for a checkout with `git`, `uv` and network access. A
consumer that vendors revl by git ref has none of those guaranteed, and the
clone cannot ride along in the export: `.cordis-py/` is listed in
`backends/python/.gitignore`, so `git archive <ref>` never carries it and the
first `import cordis` fails with `ModuleNotFoundError: No module named
'cordis'`.

The runtime is also committed to this repository as a wheel, packaged from the
same pinned clone by `site/build.py`:

```sh
pip install site/vendor/cordis-4.0.0-py3-none-any.whl
```

That is the whole installation. The wheel is pure Python (`py3-none-any`,
`Requires-Python: >=3.11`) and since issue #946 its METADATA declares `pyyaml`
and `watchdog`, the two packages `import cordis` needs at module scope, so pip
resolves them for you. `revl doctor` then reports `cordis-py runtime 4.0.0` and
`revl run --backend py` boots a composition.

One export note: the wheel lives under `site/vendor/`, so
`git archive <ref> backends/python` on its own does not contain it. Export the
whole ref, add `site/vendor` to the paths you export, or copy the wheel across
by hand.

**PyPI's `cordis` is a different project.** It is an unrelated placeholder, so
`pip install cordis` reports success and still leaves the runtime missing.
Install the committed wheel, or run `setup.sh`.

## Signature verification dependency

The Ed25519 and RSA (RS256, RSASSA-PKCS1-v1_5 with SHA-256) signature checks in
`tests/test_signature_verification.py` use `cryptography` 50.0.2, pinned and
hashed in `requirements.txt`. `setup.sh` installs that file with
`uv pip install --require-hashes`, so a wheel whose sha256 is not in the lock
cannot be installed in place of the pinned release. The lock is a universal
resolution: one file carries the hashes for macOS arm64, Linux x86_64 and
Windows, across the CPython versions the resolution covers (3.11 through 3.15,
including the free-threaded 3.14t and 3.15t variants).

| platform | cryptography wheel for CPython 3.11+ | sha256 |
|---|---|---|
| macOS arm64 | `cryptography-50.0.2-cp311-abi3-macosx_11_0_arm64.whl` | `fa8f5efb344d6908a1ce62f4a24e2e5780f825d6f53f5f50ec5ffacac72936cb` |
| Linux x86_64 | `cryptography-50.0.2-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` | `630ebfea3bf689d075f82316324ff7433dc447fe6bc1bfc76524b74b4a9567d2` |
| Windows x86_64 | `cryptography-50.0.2-cp311-abi3-win_amd64.whl` | `7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c` |

The lock also hashes the cp39-abi3 wheels (`0ec5f09541743261e66e291b4a0cbf0fb2997aeaab6d9e9c740b9dba1b58d1c2`
for macOS arm64, `ee247f5c245c9a2fe7c8e2214e295918838e44e00a45a6718451e4004219e767`
for Linux x86_64 and `4e81d95e5bafc2d6e34e4bed780e53e4d5b9a2f928573428aa4d35fbec1eb0de`
for Windows), so a resolver that picks those installs the same pinned release.

`cffi` 2.1.1 is a per-interpreter dependency: the lock carries its wheels for
each CPython version the resolution covers, 3.11 through 3.15 plus the
free-threaded variants. For the CPython 3.12 wheel this backend's CI uses, the
filenames and hashes are:

| platform | cffi wheel | sha256 |
|---|---|---|
| macOS arm64 | `cffi-2.1.1-cp312-cp312-macosx_11_0_arm64.whl` | `f81b3b8f3d4e343550fa4baa0e479bba9f2d29ce9c2e9b51d1ce1718d7442fcf` |
| Linux x86_64 | `cffi-2.1.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` | `c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf` |
| Windows x86_64 | `cffi-2.1.1-cp312-cp312-win_amd64.whl` | `f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a` |

`pycparser` 3.12 is the pure-Python fallback cffi needs when no compiled CFFI
is available: `pycparser-3.12-py3-none-any.whl`, sha256
`a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e`.

Windows support is recorded from wheel availability and from two paths in this
repository: `setup.sh`'s `.venv/Scripts` branch, and the nightly
`windows-smoke` workflow (`.github/workflows/windows-smoke.yml`), which runs
`backends/python/setup.sh` on `windows-latest`, so the win_amd64 lock path is
exercised there. That workflow is nightly and on demand, not a pull request
gate. A clean install downloads about 4.2 MB of prebuilt wheels on macOS arm64
and about 5.0 MB on Linux x86_64 (cryptography is 3.9 to 4.8 MB depending on
platform, cffi about 0.2 MB, pycparser about 0.06 MB) and needs no compiler or
toolchain.

To regenerate the lock after a version bump, from the repository root:

```sh
printf 'cryptography==50.0.2\n' > /tmp/req.in
uv pip compile --universal --generate-hashes --python-version 3.11 \
  /tmp/req.in -o backends/python/requirements.txt
```

This reproduces every pin and sha256 below. The regenerated file differs only in
the autogenerated header comment and in the `# via` input-path annotation; the
requirements and hashes are byte-identical.

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
