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


SPAWN = HERE / "scenarios" / "emitted" / "spawn" / "spawn.ir.json"


def test_spawn_lowers_to_isolated_child_fiber():
    # Instance-parametric `spawn` (docs/design-v2-instances.md, phase 1) is now
    # lowered, not rejected. The executable proof lives in
    # scenarios/emitted/spawn/gen_exec_test.go; here we assert the structure.
    src = emit.emit(_load(SPAWN), package="spawn")
    # The handle type + per-target plug helper are emitted.
    assert "type RevlSpawnHandle struct {" in src
    assert "func revlSpawnWorker(parent *stc.Context, cfg WorkerConfig) *RevlSpawnHandle {" in src
    # Each provided key is isolated into a FRESH LOCAL realm (a distinct
    # *stc.Realm minted per call — NOT interned by name like a global realm).
    assert "child := parent.Child()" in src
    assert "child.Isolate(_keyCounter, stc.NewRealm(stc.RootRealm()," in src
    # The template is plugged as a CHILD FIBER of the spawner.
    assert "fiber := child.Load(Worker(cfg))" in src
    assert "return newRevlSpawnHandle(fiber, child)" in src
    # The spawn acquisition binds a handle, and config flows through the spawn.
    assert "var w1 *RevlSpawnHandle" in src
    assert 'w1 = revlSpawnWorker(ctx, WorkerConfig{Tag: "a", Id: 1})' in src
    # The handle's inverse (`undo w1.dispose()`) tears the instance down.
    assert "return func() error { w1.Dispose(); return nil }" in src


def test_spawn_handle_dispose_is_idempotent():
    # Dispose takes the fiber exactly once (nil-guarded), so the spawner's own
    # undo is a harmless no-op once the instance is already gone.
    src = emit.emit(_load(SPAWN), package="spawn")
    assert "func (h *RevlSpawnHandle) Dispose() error {" in src
    assert "h.fiber = nil" in src


def test_no_spawn_no_handle_type():
    # The handle and helpers are emitted ONLY when the document spawns, so
    # non-spawning programs stay byte-identical.
    src = emit.emit(_load(USER_CACHE), package="usercache")
    assert "RevlSpawnHandle" not in src
    assert "revlSpawn" not in src


def test_ir_version_gate():
    # v1/v2/v3 are accepted; anything else is refused.
    with pytest.raises(emit.EmitError):
        emit.emit({"ir_version": 4, "components": []})


# --------------------------------------------------------------------------
# ir_version 3 (pure / typed-core tier) — the executable target lives under
# backends/go/v3/ and runs on real Go (`go test`); these assert the emitter's
# shape and that the checked-in Go is a current, reproducible emit.
# --------------------------------------------------------------------------

V3_FIXTURES = ROOT / "backends" / "typescript" / "tests" / "fixtures"
V3_TESTS = V3_FIXTURES / "v3_tests.ir.json"
V3_TYPES_FUNCTIONS = V3_FIXTURES / "v3_types_functions.ir.json"
V3_STDLIB = V3_FIXTURES / "v3_stdlib.ir.json"

# The checked-in Go is gofmt'd; a fresh emit is not. gofmt only moves
# whitespace (it never reorders or rewrites tokens), so comparisons here strip
# ALL whitespace — a real structural drift still shows, but `interface {` vs
# gofmt's `interface{` does not.
_ws = lambda s: "".join(s.split())


def _has(src, needle):
    assert _ws(needle) in _ws(src), f"missing (ws-insensitive): {needle!r}"


def test_v3_tests_emit_shapes():
    src = emit.emit(_load(V3_TESTS), package="tests")
    assert "ir_version 3" in src
    assert "package tests" in src
    _has(src, "func add(a int64, b int64) int64")
    # `test` block -> real Go test; the receiver is renamed so a user binding
    # named `t` cannot shadow *testing.T.
    _has(src, "func TestAddWorks(revlT *testing.T)")
    _has(src, "revlT.Fatalf")


def test_v3_types_functions_emit_shapes():
    src = emit.emit(_load(V3_TYPES_FUNCTIONS), package="types_functions")
    _has(src, "type Row struct {")
    # user ADT -> sealed interface + case structs + type-switch match
    _has(src, "type Outcome interface { isOutcome() }")
    _has(src, "type OutcomeOk struct { Value Row }")
    _has(src, "switch _m := outcome.(type) {")
    _has(src, "case OutcomeOk:")
    # if-expression -> IIFE; list literal element type
    _has(src, "func() int64 {")
    _has(src, "[]int64{1, 2, 3}")
    # extern (falls back to @ts body, valid Go here)
    _has(src, "func greet(name string) string")


def test_v3_stdlib_emit_shapes():
    src = emit.emit(_load(V3_STDLIB), package="stdlib")
    # Opt/Result as generic sealed interfaces
    _has(src, "type RevlOpt[T any] interface { isRevlOpt() }")
    _has(src, "type RevlResult[T any, E any] interface { isRevlResult() }")
    # optfield/optcall -> revlOptMap over the payload
    _has(src, "revlOptMap(row, func(_x Row) string { return _x.name })")
    _has(src, "revlOptMap(s, func(_x string) int64 { return revlStrCharCodeAt(_x, 0) })")
    # stdlib builtins dispatch Str vs List
    _has(src, "revlStrLen(s)")
    _has(src, "revlListPush(xs, x)")
    # template -> fmt.Sprintf; arrow -> Go closure; Result construction
    _has(src, 'fmt.Sprintf("hi %v#%v!", name, n)')
    _has(src, "func(y int64) int64 { return (y + 1) }")
    _has(src, "RevlOk[int64, string]{Value: n}")


def _gofmt(src: str) -> str | None:
    """gofmt `src`, or None when gofmt is unavailable. gofmt also strips the
    redundant parens the expression renderer emits around `if`/`for`
    conditions, so this is what makes the checked-in file byte-reproducible."""
    import shutil
    import subprocess
    if shutil.which("gofmt") is None:
        return None
    proc = subprocess.run(
        ["gofmt"], input=src, capture_output=True, text=True)
    assert proc.returncode == 0, f"gofmt rejected the emit:\n{proc.stderr}"
    return proc.stdout


@pytest.mark.parametrize("ir_path,pkg,rel", [
    (V3_TESTS, "tests", "v3/tests/gen_test.go"),
    (V3_TYPES_FUNCTIONS, "types_functions", "v3/types_functions/gen.go"),
    (V3_STDLIB, "stdlib", "v3/stdlib/gen.go"),
])
def test_v3_checked_in_generated_is_current(ir_path, pkg, rel):
    """The committed v3 Go is byte-identical to gofmt(emit(ir)) — this is the
    reproducibility gate regen.sh guarantees."""
    formatted = _gofmt(emit.emit(_load(ir_path), package=pkg))
    committed = (HERE / rel).read_text(encoding="utf-8")
    if formatted is None:  # no gofmt: fall back to a whitespace-insensitive check
        assert _ws(emit.emit(_load(ir_path), package=pkg)) == _ws(committed), (
            f"{rel} is stale — run backends/go/regen.sh")
        return
    assert formatted == committed, f"{rel} is stale — run backends/go/regen.sh"


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


# ---- Map value type (docs/stdlib-2.0.md §Map) ------------------------------

def test_map_value_helpers_are_pulled_in_and_persistent():
    src = emit.emit(_compile('pub fn put(m: Map[Str, Int], k: Str, v: Int)'
                             ' -> Map[Str, Int] { return m.set(k, v) }'))
    assert "func revlMapSet[K comparable, V any](m map[K]V, k K, v V) map[K]V {" in src
    assert "revlMapSet(m, k, v)" in src
    # the helper copies before it puts — the receiver never mutates
    helper = src.split("func revlMapSet")[1].split("\n}\n")[0]
    assert "out := make(map[K]V, len(m)+1)" in helper
    assert "for kk, vv := range m" in helper


def test_map_lookup_answers_the_sealed_opt_and_has_bool():
    src = emit.emit(_compile(
        'pub fn get(m: Map[Str, Int], k: Str) -> Int { return m.lookup(k) ?? 0 }\n'
        'pub fn member(m: Map[Str, Int], k: Str) -> Bool { return m.has(k) }\n'))
    assert "revlMapGet(m, k)" in src and "revlMapHas(m, k)" in src
    # lookup answers a RevlOpt, so the Opt preamble is pulled in for `??`
    assert "type RevlOpt[T any] interface{ isRevlOpt() }" in src
    assert "revlOptOr(revlMapGet(m, k), 0)" in src


def test_map_empty_renders_positionally_or_refuses_honestly():
    """Go infers composite literals from position, not later use. A typed
    return pins the literal; an unpinned binding is refused rather than
    emitted as non-compiling Go (docs/stdlib-2.0.md §Map)."""
    src = emit.emit(_compile(
        'pub fn newTable() -> Map[Str, Int] { return Map.empty() }'))
    assert "return map[string]int64{}" in src
    with pytest.raises(emit.EmitError, match="untyped empty Map"):
        emit.emit(_compile('pub fn f() -> Int { var m = Map.empty() return 0 }'))


# --------------------------------------------------------------------------
# v3 typed-core placement (FR-8 follow-up): a v3 composition with components
# AND top-level types/functions places on the go backend — the typed-core tier
# (records/ADTs/pure fns) next to the live stc-go components, plus a bridge
# that carries records (json-tagged structs) and variants ({"$kind","$value"})
# across the seam. This is `emit_placement`, the source the runner's `emitted`
# package is built from.
# --------------------------------------------------------------------------

V3_STEP_IR = ROOT / "examples" / "v3_step_scheduler.rvl"


def _compile_ir(path: Path) -> dict:
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from revl import compile_files
    return compile_files([str(path)])


def _placement_split(ir: dict):
    """emit_placement -> (gen.go, bridge_gen.go) via the form-feed sentinel."""
    src = emit.emit_placement(ir, package="emitted")
    assert "\f" in src, "emit_placement must separate gen from bridge"
    return src.split("\f", 1)


def test_v3_typed_core_placement_emits_types_and_live_components():
    """The combined module carries BOTH the typed-core tier and the stc-go
    components — before this, emit_placement refused v3 typed-core documents
    outright ("placement on the go backend needs v1/v2 services")."""
    ir = _compile_ir(V3_STEP_IR)
    gen, bridge = _placement_split(ir)
    # typed-core tier: records with EXPORTED json-tagged fields (the bridge's
    # plain-JSON wire encoding needs them; the pure tier keeps unexported
    # fields byte-for-byte) + the ADT sealed interface
    _has(gen, "type Row struct {")
    _has(gen, 'Id int64 `json:"id"`')
    _has(gen, "type Step interface { isStep() }")
    _has(gen, "type StepFinal struct { Value string }")
    # live stc-go components: service interface, key, impl, load helper
    _has(gen, "type Scheduler interface {")
    _has(gen, "Next(now Step) Step")
    _has(gen, 'stc.NewKey[Scheduler]("sched")')
    _has(gen, "func LoadSched(target *stc.Context) *stc.Fiber {")
    # method bodies: record literal, ADT construction, ADT match (type switch)
    _has(gen, 'return Row{Id: 1, Name: "ada"}')
    _has(gen, "return StepFinal{Value: msg}")
    _has(gen, "switch _m := now.(type) {")
    _has(gen, "case StepFinal:")
    # bridge: variant encode/decode helpers + wired proxy/dispatch
    _has(bridge, "func _revlEncodeStep(v Step) any {")
    _has(bridge, 'map[string]any{"$kind": "Final", "$value": c.Value}')
    _has(bridge, "func _revlDecodeStep(raw json.RawMessage) Step {")
    _has(bridge, "_revlEncodeStep(now)")
    _has(bridge, "_r := _revlDecodeStep(_v)")


def test_v3_typed_core_placement_host_collision_is_renamed():
    """A declared record named like a legacy host-runtime type (Row) renames
    the HOST side to RevlRow, so the two never collide in one package."""
    ir = _compile_ir(V3_STEP_IR)
    gen, _ = _placement_split(ir)
    assert "type RevlRow = map[string]string" in gen
    assert "type Row struct {" in gen


def test_v3_typed_core_placement_records_round_trip_through_the_bridge():
    """outcome.rvl's boundary shapes — a user ADT return, a Result[Row, Str]
    return, and a record return — get decode/encode wired on both sides."""
    ir = _compile_ir(ROOT / "examples" / "outcome.rvl")
    gen, bridge = _placement_split(ir)
    _has(gen, "type Found interface { isFound() }")
    _has(gen, "type FoundHit struct { Value Row }")
    _has(bridge, "func _revlEncodeFound(v Found) any {")
    _has(bridge, "func _revlDecodeFound(raw json.RawMessage) Found {")
    # Result[Row, Str] carries the record payload through $value (json tags)
    _has(bridge, 'return map[string]any{"$kind": "Ok", "$value": _okv}, nil')
    _has(bridge, "var _ok Row")


def test_v3_pure_document_still_refuses_placement():
    """A v3 document with no components has nothing to boot — placement stays
    an honest refusal (the pure typed-core tier is `revl test` territory)."""
    from revl import compile_source
    ir = compile_source("pub fn add(a: Int, b: Int) -> Int { return a + b }")
    with pytest.raises(emit.EmitError, match="no components"):
        emit.emit_placement(ir, package="emitted")


def _compile(source: str) -> dict:
    """revl source -> IR (the go test file only ever loaded checked-in IR)."""
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from revl import compile_source
    return compile_source(source)
