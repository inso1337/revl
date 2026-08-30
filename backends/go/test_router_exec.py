"""Multi-realm routing (item 173) on the cordis-go tier.

Two layers:

* `test_emit_*` — pure emitter assertions, no toolchain. The Router lowers a
  routed require into an emitted router struct that consumes the stc-go fork's
  strict single-realm read; routed keys are excluded from `Inject`; a routes-less
  program emits byte-identically.
* `test_go_router_*` — BUILD + RUN the emitted router against the stc-go fork
  (`forks/stc-go`, which carries the item-173 `ServiceInRealm` primitive) with
  a `replace` directive, proving round-robin distribution, failover, and G2 at
  runtime. Skips loudly without a `go` toolchain or the fork.

The upstream module cache does NOT carry the primitive, so the runtime layer is
gated on the in-repo fork; that is the honest reachable-here proof for a tier
whose runtime lands upstream via fork + PR (forks/stc-go/REVL-FORK.md).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
FORK = ROOT / "forks" / "stc-go"
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SCENARIO = (BACKEND / "scenarios" / "router.rvl").read_text()


def _emit(source: str) -> str:
    spec = importlib.util.spec_from_file_location("revl_go_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source))


def test_emit_router_struct_and_strict_read():
    go = _emit(SCENARIO)
    assert "type revlRouterRouterWorker struct" in go
    assert "func newRevlRouterRouterWorker(ctx *stc.Context)" in go
    # the strict single-realm liveness-checked read (the fork's primitive)
    assert "stc.ServiceInRealm[Worker](r.ctx, r.key, _revlRealm(realm))" in go
    # forwards each op through a fresh selection (failover from the body)
    assert "func (r *revlRouterRouterWorker) Call(request string) string" in go
    assert "r._revlSelect().Call(request)" in go


def test_emit_routed_key_excluded_from_inject():
    go = _emit(SCENARIO)
    # the Router requires `worker` but it is ROUTED — it must NOT appear in an
    # Inject gate (no single-realm provider), or the Router would hang Pending.
    assert "Inject: []stc.Key{_keyWorker}" not in go
    # and it is wired as a router value, not a stc.Service handle.
    assert "worker := newRevlRouterRouterWorker(ctx)" in go


def test_emit_routeless_program_is_byte_identical():
    """A program with no routed require emits identically with and without the
    item-173 code paths present — the additive-gate guarantee."""
    plain = """
    service Greeter { fn hi(name: Str) -> Str }
    component G provides greeter: Greeter {
      provide greeter { fn hi(name) = "hi " + name }
    }
    """
    go = _emit(plain)
    assert "revlRouter" not in go
    assert "ServiceInRealm" not in go
    assert "revl:routes" not in go


# --------------------------------------------------------------------------
# runtime layer: build + run against the stc-go fork
# --------------------------------------------------------------------------

def _go_or_skip() -> str:
    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not found")
    if not (FORK / "route.go").exists():
        pytest.skip(f"stc-go fork with the item-173 primitive not found at {FORK}")
    return go


def _build_dir(tmp_path: Path) -> Path:
    go_code = _emit(SCENARIO)
    (tmp_path / "emitted.go").write_text(go_code)
    shutil.copy(BACKEND / "scenarios" / "router_test.go.fixture",
                tmp_path / "router_test.go")
    (tmp_path / "go.mod").write_text(
        "module revl173gorouter\n\n"
        "go 1.25.0\n\n"
        "require github.com/0xdenny218/stc-go v0.6.1\n\n"
        f"replace github.com/0xdenny218/stc-go => {FORK}\n"
    )
    gosum = FORK / "go.sum"
    if gosum.exists():
        shutil.copy(gosum, tmp_path / "go.sum")
    return tmp_path


def test_go_router_builds_and_runs(tmp_path):
    go = _go_or_skip()
    build = _build_dir(tmp_path)
    proc = subprocess.run(
        [go, "test", "-run", "TestGoRouter", "./..."],
        cwd=build, capture_output=True, text=True,
        env={"GOFLAGS": "-mod=mod", "PATH": __import__("os").environ.get("PATH", ""),
             "HOME": str(Path.home())},
    )
    assert proc.returncode == 0, f"go test failed:\n{proc.stdout}\n{proc.stderr}"
