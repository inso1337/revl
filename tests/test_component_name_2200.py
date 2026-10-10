"""One component-name conversion, and an unknown component is fatal (#2200).

Two PascalCase -> snake_case conversions existed for a component name: the
placement spec's own (``src/revl/placement.py``: split before every capital,
``KVStore`` -> ``k_v_store``) and the rust emitter's (``backends/rust/emit.py``:
a separator only after a lowercase letter or digit, ``KVStore`` -> ``kvstore``).
The emitter's spelling is the one that counts — it is the ``pub fn`` name and
the key the runner's generated plugin table (``components::_revl_load``) is
matched on — so a component whose name has consecutive capitals was placed under
a name the rust build does not have.

That mismatch did not fail. ``backends/rust/placement_runner/src/main.rs``
logged ``UNKNOWN component`` for the missing name and *continued*, so the
composition booted one component short, still printed ``UP`` and
``NO-RESIDUE``, and `revl run p.rvl --backend rust --once` exited 0 — a rust run
that silently skipped a component and claimed success.

(a) and (b) below pin the conversion to the emitter's own function. (c) pins the
runner's lookup: a source assertion that runs everywhere, and — where a
cordis-rs toolchain is present — a real build-and-run proof, because the bug is
a *runtime* one: a source assertion alone would not show the run exits nonzero.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _paths, placement  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

#: The names the audit measured, four with consecutive capitals (where the two
#: algorithms disagreed) and two controls they agreed on.
NAMES = ["KVStore", "HTTPServer", "DBPool", "IOHub", "KvStore", "Store"]

_RUST_REASON = rust_runtime_reason()
needs_cordis_rs = pytest.mark.skipif(
    _RUST_REASON is not None,
    reason=f"needs a resolvable cordis-rs toolchain: {_RUST_REASON}")

_RUNNER_DIR = ROOT / "backends" / "rust" / "placement_runner"
_RUNNER_BIN = _RUNNER_DIR / "target" / "debug" / "revl_placement_runner"
_MAIN_RS = _RUNNER_DIR / "src" / "main.rs"


def _rust_emit():
    """The rust emitter module, loaded from its file HERE — not through the
    loader the code under test uses — so the comparison is against the emitter
    as it stands on disk."""
    path = ROOT / "backends" / "rust" / "emit.py"
    spec = importlib.util.spec_from_file_location("revl_test_2200_rust_emit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- (a) spelling

def test_placement_spells_a_component_the_way_the_rust_emitter_does():
    """The placement spec's component name is the emitter's component name, for
    every name — including the consecutive-capital ones the two algorithms
    disagreed on."""
    emit = _rust_emit()
    for name in NAMES:
        assert placement._snake(name) == emit._snake(name), (
            f"the placement spec would place `{name}` as "
            f"`{placement._snake(name)}` while the rust emitter emits the plugin "
            f"`{emit._snake(name)}`: the runner's `_revl_load` looks the "
            f"component up by the emitted name and finds nothing")

    # the four the audit measured, pinned to the emitter's spelling rather than
    # to placement's historical `k_v_store`/`h_t_t_p_server`/... spelling.
    assert [placement._snake(n) for n in NAMES[:4]] == [
        "kvstore", "httpserver", "dbpool", "iohub"]
    assert [placement._snake(n) for n in NAMES[4:]] == ["kv_store", "store"]


# ------------------------------------------------------------- (b) single source

def test_the_conversion_placement_runs_is_the_emitters_own_function():
    """Single-source, pinned BEHAVIOURALLY: placement answers from the emitter
    module's `_snake`, so replacing that attribute changes placement's answer.
    A second copy of the algorithm inside placement would keep answering from
    its own code and this fails — which is the assertion that pins the
    duplication is gone (the spelling test above would still pass if placement
    called a *copy* of the emitter's algorithm)."""
    loader = getattr(placement, "backend_emit_module", None)
    assert loader is not None, (
        "placement does not take the rust emitter's own conversion: the loader "
        "it shares with the emitter (`revl.placement.backend_emit_module`) is "
        "missing, so placement must be carrying a second algorithm")
    module = loader("rust")
    assert Path(module.__file__).resolve() == (ROOT / "backends" / "rust" / "emit.py").resolve()
    # one module per backend, cached: placement and this test share it
    assert module is placement.backend_emit_module("rust")
    # the loader is in the placement layer, off the compile graph: a path-loading
    # call in a compile-reachable module (`revl._paths` is one, `compiler.py`
    # imports it) would add an edge to every `revl.*` module any backend file
    # imports, which `tests/test_affected_tests.py` refuses.
    assert not hasattr(_paths, "backend_emit_module"), (
        "the loader is back in `revl._paths`, which is compile-reachable: "
        "the affected-test selector would gain an edge from it to every "
        "`revl.*` module any backend file imports")

    original = module._snake
    module._snake = lambda name: "sentinel-2200"
    try:
        assert placement._snake("KVStore") == "sentinel-2200", (
            "placement._snake answered without calling the rust emitter's "
            "`_snake`: a second implementation is back")
    finally:
        module._snake = original


def test_placement_no_longer_carries_the_other_algorithm():
    """The old algorithm's own tell — `re.sub(r"(?<!^)(?=[A-Z])", "_", name)` —
    is gone from the module, and `_snake` is still the one place a component
    name is converted."""
    src = inspect.getsource(placement)
    assert "(?<!^)" not in src, "the second (pre-every-capital) conversion is back"
    assert src.count("def _snake(") == 1


# -------------------------------------------------- (c) unknown component fatal

def test_the_runner_does_not_log_past_an_unknown_component():
    """Source assertion (runs everywhere): the `None` arm of the load lookup
    exits nonzero instead of logging `UNKNOWN component` and carrying on."""
    src = _MAIN_RS.read_text(encoding="utf-8")
    assert 'log("load", cname, "UNKNOWN component")' not in src, (
        "the runner still logs an unknown component and continues")

    lookup = src.index("_revl_load(&root, cname")
    tail = src[lookup:]
    end = tail.find("// 3. serve")
    arm = tail[:end if end != -1 else 1200]
    assert "fatal_boot(" in arm or "std::process::exit(1)" in arm, (
        "the unknown-component arm does not exit nonzero: the run would claim "
        "success having skipped the component")
    if "fatal_boot(" in arm:
        start = src.index("let fatal_boot = ")
        stop = src.find("\n    };", start)
        helper = src[start:stop if stop != -1 else start + 1200]
        assert "std::process::exit(1)" in helper, (
            "the arm defers to a helper that does not exit nonzero")


def _built_runner() -> Path:
    """Build and return the real runner binary. `cargo build` is incremental,
    so this is a no-op when the tree is already built and — unlike a
    "build if the binary is missing" check — it cannot hand back a binary
    compiled from an earlier revision of `main.rs`."""
    build = subprocess.run(
        ["cargo", "build", "--manifest-path", str(_RUNNER_DIR / "Cargo.toml")],
        capture_output=True, text=True, timeout=1800)
    assert build.returncode == 0, f"cargo build failed:\n{build.stderr[-2000:]}"
    assert _RUNNER_BIN.exists(), f"no runner binary at {_RUNNER_BIN}"
    return _RUNNER_BIN


@needs_cordis_rs
def test_an_unknown_component_exits_nonzero_before_up(tmp_path):
    """Runtime proof, where a cordis-rs toolchain is present: build the real
    runner and hand it a spec naming a component the generated plugin table
    does not have. Before #2200 it logged `UNKNOWN component`, booted the rest,
    printed `UP`/`NO-RESIDUE` and exited 0."""
    binary = _built_runner()

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "unknown2200",
        "backend": "rust",
        "components": ["definitely_absent_2200"],
        "config": {},
        "provides": [],
        "proxies": {},
        "placements": {},
        "probe": [],
        "once": True,
    }), encoding="utf-8")
    run = subprocess.run([str(binary), str(spec)],
                         capture_output=True, text=True, timeout=300)

    assert run.returncode != 0, (
        f"an unknown component exited {run.returncode}: the run skipped it and "
        f"claimed success\nstdout:\n{run.stdout}\nstderr:\n{run.stderr}")
    assert "unknown component definitely_absent_2200" in run.stderr
    assert "UP" not in run.stdout


@needs_cordis_rs
def test_a_known_component_still_boots(tmp_path):
    """The positive control for the proof above: the same spec, naming a plugin
    the committed generated module does have, boots — so the nonzero exit is
    the lookup and not the spec shape."""
    binary = _built_runner()
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "known2200",
        "backend": "rust",
        "components": ["pg_database", "user_cache"],
        "config": {"PgDatabase": {"url": "sqlite::memory:", "pool_size": 10}},
        "provides": ["db", "cache"],
        "proxies": {},
        "placements": {},
        "probe": [],
        "once": True,
    }), encoding="utf-8")
    run = subprocess.run([str(binary), str(spec)],
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count("state=Active") == 2, run.stdout + run.stderr
