"""Cross-tier RUNTIME conformance gate for revl's multi-tenancy contract.

revl compiles to five runtime tiers (cordis-py reference, cordis TypeScript,
cordis-rs, cordis4j, cordis-wasm). ``docs/design-v2-realms.md`` states the
realm contract:

  * "A realm label is a static string literal. **Equal strings = same realm**."
  * "**G2 becomes per-(key, realm)**: two providers of `db` in *different*
    realms is the feature; **the same realm is the conflict**, and the error
    names the realm."

The existing per-tier realm tests (backends/python/tests/test_v2_realms.py,
backends/typescript/tests/v2_realms.test.ts) assert only the SEPARATION
direction (distinct labels -> distinct providers). The SHARING direction —
that two entries naming the SAME realm string collapse onto ONE realm, so a
second provider of that key in that realm is REFUSED — was asserted at runtime
NOWHERE, and on cordis4j the shipped behaviour contradicts it. This gate
closes that hole. See docs/notes/runtime-parity-local-realms.md (reconnaissance
on branch agent/runtime-parity).

For each tier it compiles small revl programs (tests/fixtures/realm_conformance)
and EXECUTES the emitted code on that tier's real runtime, asserting:

  (S) SEPARATION: provider in realm("shared") and provider in realm("other")
      are distinct providers, both live; disposing one does not affect the
      other.
  (H) SHARING/CONFLICT [the untested direction]: two providers of `kv` both in
      realm("shared") -> equal strings = same realm -> the second is REFUSED
      as a per-(key, realm) G2 conflict. Asserted on RUNTIME behaviour (the
      second provider's fiber is not Active / plug raises), not on emitted text.

Because the two same-realm providers cannot be co-linked (revl's linker rejects
that pair at admission — examples/rejections/v2_same_realm_conflict.rvl), each
provider is compiled as its own unit and the units are combined only in the
emitted artifact, so the RUNTIME is what must enforce (H).

A tier whose toolchain is unavailable here SKIPS with a specific reason (never
passes silently). cordis4j is expected to FAIL (H) — that divergence is a
tracked strict-xfail, so fixing the Java tier flips it to XPASS and forces the
marker's removal.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from copy import deepcopy
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# repo layout: run against THIS worktree's sources, with the beside-the-main-
# checkout clones/venvs (cordis-py venv, TS node_modules) as read-only runtime
# dependencies when the worktree does not carry its own.
# --------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "realm_conformance"
HARNESS = FIXTURES / "harness"

if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from revl import compile_files  # noqa: E402


def _common_root() -> Path | None:
    """The main checkout beside this worktree (parent of the shared git dir)."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if common.returncode != 0:
        return None
    root = Path(common.stdout.strip())
    if not root.is_absolute():
        root = (REPO_ROOT / root).resolve()
    return root.parent


def _roots() -> list[Path]:
    """Candidate checkouts, worktree first, deduped."""
    roots = [REPO_ROOT]
    common = _common_root()
    if common is not None and common != REPO_ROOT:
        roots.append(common)
    return roots


PROVIDER_A = FIXTURES / "provider_a.rvl"      # SharedStoreA, realm("shared")
PROVIDER_B = FIXTURES / "provider_b.rvl"      # SharedStoreB, realm("shared")
PROVIDER_O = FIXTURES / "provider_other.rvl"  # SharedStoreOther, realm("other")


def _load_emitter(backend: str) -> types.ModuleType:
    """Load a backend emitter under a unique name (bare `import emit` collides
    across backends), from THIS worktree."""
    path = REPO_ROOT / "backends" / backend / "emit.py"
    spec = importlib.util.spec_from_file_location(f"revl_{backend}_emit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _merged_ir() -> dict:
    """The three providers compiled as separate units, combined into one IR
    (which the linker would refuse if it saw the same-realm pair together)."""
    a = compile_files([str(PROVIDER_A)])
    b = compile_files([str(PROVIDER_B)])
    o = compile_files([str(PROVIDER_O)])
    assert a["ir_version"] == 2, "fixtures must exercise v2 realm constructs"
    merged = deepcopy(a)
    merged["components"] = a["components"] + b["components"] + o["components"]
    return merged


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600, **kw)


def _rc_json(stdout: str) -> dict:
    """Parse the single `RC_JSON {...}` verdict line a runner prints."""
    for line in stdout.splitlines():
        if line.startswith("RC_JSON "):
            return json.loads(line[len("RC_JSON "):])
    raise AssertionError(f"no RC_JSON verdict in runner output:\n{stdout}")


def _assert_contract(result: dict, tier: str) -> None:
    """The shared contract assertion for a tier's parsed verdict."""
    assert result["S"]["verdict"] == "SEPARATE", (
        f"{tier}: SEPARATION failed — distinct realm strings must resolve to "
        f"distinct, independently-disposable providers; got {result['S']}"
    )
    assert result["H"]["verdict"] == "REFUSED", (
        f"{tier}: SHARING/CONFLICT failed — equal realm strings denote the "
        f"same realm, so the second provider of `kv` in realm(\"shared\") must "
        f"be REFUSED (docs/design-v2-realms.md: 'the same realm is the "
        f"conflict'); got {result['H']}"
    )


# ==========================================================================
# cordis-py (reference)
# ==========================================================================

def _python_with_cordis() -> tuple[str, str] | None:
    """(interpreter, backends/python dir) for a python that can import cordis,
    honouring env override then worktree/main-checkout backend venvs."""
    candidates: list[Path] = []
    override = os.environ.get("REVL_CONFORMANCE_PY")
    if override:
        candidates.append(Path(override))
    for root in _roots():
        candidates.append(root / "backends" / "python" / ".venv" / "bin" / "python")
    for interp in candidates:
        if not interp.exists():
            continue
        probe = _run([str(interp), "-c", "import cordis"])
        if probe.returncode == 0:
            return str(interp), str(REPO_ROOT / "backends" / "python")
    return None


def test_cordis_py_realm_conformance(tmp_path):
    found = _python_with_cordis()
    if found is None:
        pytest.skip(
            "cordis-py: no venv with cordis importable "
            "(run backends/python/setup.sh, or set REVL_CONFORMANCE_PY)"
        )
    interp, backend_dir = found
    emit = _load_emitter("python")

    # Emit each provider from its own single-component IR (as separately
    # compiled units). They share one process-wide runtime.realm_label
    # registry at run time, so equal realm strings still intern to one realm.
    (tmp_path / "a.py").write_text(emit.emit(compile_files([str(PROVIDER_A)])), encoding="utf-8")
    (tmp_path / "b.py").write_text(emit.emit(compile_files([str(PROVIDER_B)])), encoding="utf-8")
    (tmp_path / "other.py").write_text(emit.emit(compile_files([str(PROVIDER_O)])), encoding="utf-8")

    proc = _run([interp, str(HARNESS / "harness_py.py"), str(tmp_path), backend_dir])
    assert proc.returncode == 0, f"runner failed:\n{proc.stdout}\n{proc.stderr}"
    result = _rc_json(proc.stdout)
    _assert_contract(result, "cordis-py")


# ==========================================================================
# cordis (TypeScript)
# ==========================================================================

def _node_strips_types() -> bool:
    """Node >=22.6 strips type annotations from a .ts file by extension (the
    `-e` inline form does not, so probe with a real file)."""
    node = shutil.which("node")
    if node is None:
        return False
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        probe_ts = Path(d) / "probe.ts"
        probe_ts.write_text("const x: number = 41; console.log(x + 1)\n", encoding="utf-8")
        probe = _run([node, str(probe_ts)])
    return probe.returncode == 0 and probe.stdout.strip() == "42"


def _ts_backend_dir() -> Path | None:
    """A backends/typescript dir whose node_modules has cordis installed."""
    override = os.environ.get("REVL_CONFORMANCE_TS")
    roots = [Path(override)] if override else [r / "backends" / "typescript" for r in _roots()]
    for tsdir in roots:
        if (tsdir / "node_modules" / "cordis" / "package.json").exists():
            return tsdir
    return None


def test_cordis_ts_realm_conformance(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("cordis (TS): node not installed")
    if not _node_strips_types():
        pytest.skip("cordis (TS): node cannot run TypeScript (needs >=22.6 type stripping)")
    tsdir = _ts_backend_dir()
    if tsdir is None:
        pytest.skip("cordis (TS): node_modules/cordis not installed (run npm ci in backends/typescript)")

    emit = _load_emitter("typescript")
    runtime_ts = str((tsdir / "runtime.ts").resolve())

    def emit_one(path: Path) -> str:
        return emit.emit(compile_files([str(path)]), runtime_import=runtime_ts)

    (tmp_path / "a.ts").write_text(emit_one(PROVIDER_A), encoding="utf-8")
    (tmp_path / "b.ts").write_text(emit_one(PROVIDER_B), encoding="utf-8")
    (tmp_path / "other.ts").write_text(emit_one(PROVIDER_O), encoding="utf-8")

    runner = (HARNESS / "harness_ts.ts").read_text(encoding="utf-8").replace("__RUNTIME__", runtime_ts)
    (tmp_path / "runner.ts").write_text(runner, encoding="utf-8")
    # bare `cordis` (in the runner and in runtime.ts) resolves from here.
    os.symlink(tsdir / "node_modules", tmp_path / "node_modules")

    proc = _run([shutil.which("node"), str(tmp_path / "runner.ts")], cwd=tmp_path)
    assert proc.returncode == 0, f"runner failed:\n{proc.stdout}\n{proc.stderr}"
    result = _rc_json(proc.stdout)
    _assert_contract(result, "cordis")


# ==========================================================================
# cordis-rs
# ==========================================================================

_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode", "without the offline flag", "--offline was specified",
    "registry index was not found", "no matching package", "failed to select a version",
)
_REAL_FAILURE_MARKERS = ("error[e", "could not compile", "test result: failed", "panicked at")


def _is_offline_resolve_failure(proc: subprocess.CompletedProcess) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(m in blob for m in _REAL_FAILURE_MARKERS):
        return False
    return any(m in blob for m in _OFFLINE_RESOLVE_MARKERS)


def _crates_io_reachable() -> bool:
    import socket
    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _cargo_test(cwd: Path) -> subprocess.CompletedProcess:
    """`cargo test` — offline first (warm ~/.cargo), networked resolve only for
    a genuine crate-resolution miss (mirrors backends/rust/test_emit_rust.py)."""
    offline = _run(["cargo", "test", "--offline"], cwd=cwd)
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline
    if not _crates_io_reachable():
        pytest.skip(
            "cordis-rs: not in the local cargo registry and index.crates.io is "
            "unreachable — run once with network to populate ~/.cargo"
        )
    return _run(["cargo", "test"], cwd=cwd)


def test_cordis_rs_realm_conformance(tmp_path):
    if shutil.which("cargo") is None:
        pytest.skip("cordis-rs: cargo not installed")

    emit = _load_emitter("rust")
    merged = _merged_ir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(emit.emit(merged), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_scenarios"), encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "realms.rs").write_text(
        (HARNESS / "realms.rs").read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = _cargo_test(tmp_path)
    # a green cargo test IS the runtime verdict: both #[test]s (S and H) pass.
    assert result.returncode == 0, (
        "cordis-rs realm scenarios failed on the real runtime:\n"
        + result.stdout + "\n" + result.stderr
    )
    assert "2 passed" in result.stdout, result.stdout


# ==========================================================================
# cordis4j  — EXPECTED DIVERGENCE (strict xfail on H)
# ==========================================================================

def _working_jdk(tool: str) -> str | None:
    """A JDK binary that actually works. On macOS /usr/bin/<tool> is a shim
    that errors when no JDK is installed; the Homebrew keg is not on PATH."""
    candidates = ["/opt/homebrew/opt/openjdk/bin/" + tool, shutil.which(tool)]
    for exe in candidates:
        if not exe:
            continue
        try:
            probe = _run([exe, "-version"])
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return exe
    return None


@pytest.mark.xfail(
    reason="cordis4j: ContextImpl per-fiber ServiceRegistry.store does not "
    "implement equal-strings-share — two providers isolated into realm(\"shared\") "
    "BOTH LOAD instead of conflicting (H). See "
    "docs/notes/runtime-parity-local-realms.md; tracked for fix on "
    "agent/java-realm-fix.",
    strict=True,
)
def test_cordis4j_realm_conformance(tmp_path):
    javac = _working_jdk("javac")
    java = _working_jdk("java")
    classes = os.environ.get("REVL_CORDIS4J_CLASSES")
    if javac is None or java is None:
        pytest.skip("cordis4j: no working JDK (need OpenJDK; macOS /usr/bin/javac is a shim)")
    if not classes:
        pytest.skip(
            "cordis4j: REVL_CORDIS4J_CLASSES unset — compile cordis4j-core "
            "(github.com/1na-ko/cordis4j) and point it at the classes dir"
        )

    emit = _load_emitter("java")
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(emit.emit(_merged_ir()), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    compile_all = _run(
        [javac, "--release", "21", "-cp", classes, "-d", str(out),
         str(pkg / "Components.java"), str(HARNESS / "RunRealmConformance.java")]
    )
    assert compile_all.returncode == 0, compile_all.stderr
    proc = _run([java, "-cp", f"{classes}{os.pathsep}{out}", "RunRealmConformance"])
    assert proc.returncode == 0, proc.stderr + proc.stdout

    # Parse the H_VERDICT / S_VERDICT lines the harness prints.
    verdicts = dict(
        line.split(" ", 1) for line in proc.stdout.splitlines() if line.startswith(("H_", "S_"))
    )
    assert verdicts.get("S_VERDICT") == "SEPARATE", proc.stdout
    assert verdicts.get("H_VERDICT") == "REFUSED", (
        "cordis4j: equal realm strings must collapse to one realm and refuse "
        "the second provider; got H_VERDICT=" + str(verdicts.get("H_VERDICT")) + "\n" + proc.stdout
    )


# ==========================================================================
# cordis-wasm
# ==========================================================================

def _cordis_wasm() -> tuple[str, str] | None:
    """(venv python with wasmtime, runtime dir) for the cordis-wasm substrate."""
    cw = Path(os.environ.get("CORDIS_WASM", Path.home() / "Projects" / "cordis-wasm"))
    interp = cw / ".venv" / "bin" / "python"
    if not interp.exists():
        return None
    probe = _run([str(interp), "-c", "import wasmtime, runtime"], cwd=str(cw))
    if probe.returncode != 0:
        return None
    return str(interp), str(cw)


def test_cordis_wasm_realm_conformance(tmp_path):
    found = _cordis_wasm()
    if found is None:
        # exact string CI's wasm skip-audit exempts (.github/workflows/ci.yml)
        pytest.skip("cordis-wasm venv not available")
    interp, cw = found

    emit = _load_emitter("wasm")
    mods = emit.emit(_merged_ir())  # {component_name: wat_source}
    (tmp_path / "mods.json").write_text(json.dumps(mods), encoding="utf-8")

    proc = _run([interp, str(HARNESS / "harness_wasm.py"), str(tmp_path), cw])
    assert proc.returncode == 0, f"runner failed:\n{proc.stdout}\n{proc.stderr}"
    result = _rc_json(proc.stdout)
    _assert_contract(result, "cordis-wasm")
