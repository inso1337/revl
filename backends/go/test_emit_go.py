"""Unit tests for the cordis-go emitter (backends/go/emit.py).

These assert the emitter's structure and the isolate-at-load-site invariant.
The *executable* proof — emitted code running on the real stc-go runtime —
lives in backends/go/scenarios/emitted/ and runs under `go test`; these Python
tests are the compile-time complement.

Run: pytest backends/go/test_emit_go.py -q
"""

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_go_emit", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


USER_CACHE = ROOT / "examples" / "user_cache.ir.json"
TENANTS = ROOT / "backends" / "typescript" / "tests" / "fixtures" / "tenants.ir.json"


def test_user_cache_shapes():
    src = emit.emit(_load(USER_CACHE))
    assert "package emitted" in src
    # service interface
    assert "type Database interface {" in src
    assert "Query(sql string) []Row" in src
    # Opt[Str] return lowers to (string, bool)
    assert "Get(key string) (string, bool)" in src
    # effect + inverse
    assert "ctx.Effect(func() stc.Inverse {" in src
    assert "return func() error { pool.Close(); return nil }" in src
    # provide + inject
    assert 'stc.NewKey[Database]("db")' in src
    assert "ctx.Provide(_keyDb, Database(" in src
    assert "stc.Service[Database](ctx, _keyDb)" in src
    # config defaults
    assert "func DefaultPgDatabaseConfig() PgDatabaseConfig {" in src
    assert "PoolSize: 10," in src
    # format -> Sprintf
    assert 'fmt.Sprintf("INSERT INTO cache_log VALUES (%v)", key)' in src


def test_tenants_isolate_at_load_site():
    src = emit.emit(_load(TENANTS))
    # the realm helper is emitted once
    assert "func _revlRealm(name string) *stc.Realm {" in src
    # isolation is applied at the LOAD SITE (Load<Name>), not inside Apply —
    # this is the reactive-link fix: isolating inside Apply runs after the
    # Inject gate has already evaluated on the un-isolated context.
    assert "func LoadTenantAStore(target *stc.Context) *stc.Fiber {" in src
    assert 'ctx.Isolate(_keyKv, _revlRealm("tenant_a"))' in src
    assert 'ctx.Isolate(_keyKv, _revlRealm("tenant_b"))' in src
    # no Isolate call inside a component's Apply body
    apply_region = src.split("func LoadTenantAStore")[0]
    assert ".Isolate(" not in apply_region, "isolate must not appear inside Apply"
    # intercept metadata lowers to a real ctx.Intercept call at the load site
    assert 'ctx.Intercept(_keyKv, map[string]any{"quota": 5' in src


def test_package_name_alias():
    a = emit.emit(_load(USER_CACHE), package="usercache")
    b = emit.emit(_load(USER_CACHE), package_name="usercache")
    assert a == b
    assert "package usercache" in a


def test_spawn_ir_is_rejected():
    with pytest.raises(emit.EmitError):
        emit.emit({"ir_version": 2, "components": [{"name": "X", "spawn": {}}]})


def test_ir_version_gate():
    with pytest.raises(emit.EmitError):
        emit.emit({"ir_version": 3, "components": []})


@pytest.mark.parametrize("ir_path,pkg", [
    (USER_CACHE, "usercache"),
    (TENANTS, "tenants"),
])
def test_checked_in_generated_is_current(ir_path, pkg):
    """The committed gen.go must match a fresh emit (modulo gofmt)."""
    fresh = emit.emit(_load(ir_path), package=pkg)
    committed = (HERE / "scenarios" / "emitted" / pkg / "gen.go").read_text(encoding="utf-8")
    # Compare ignoring whitespace runs so gofmt's alignment doesn't cause
    # false diffs; a real structural drift still shows.
    norm = lambda s: " ".join(s.split())
    assert norm(fresh) == norm(committed), (
        f"{pkg}/gen.go is stale — run backends/go/regen.sh")
