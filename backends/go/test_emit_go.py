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


def test_timer_lowers_to_schedule_cancel_effect():
    """A `timer` step (item 57) lowers to a revertible schedule: the arm runs
    inside `ctx.Effect` (the effect ledger) and the derived inverse cancels it,
    so unload drains it like any other effect. The clock preamble is pulled in
    only when a timer is present."""
    ir = _load(HERE / "scenarios" / "emitted" / "timer" / "timer.ir.json")
    src = emit.emit(ir, package="timer")
    # periodic + one-shot both lower through the schedule helpers
    assert "revlScheduleEvery(30000, func() {" in src
    assert "revlScheduleAfter(300000, func() {" in src
    # armed inside the effect ledger, cancel is the yielded inverse
    assert "ctx.Effect(func() stc.Inverse {" in src
    assert "return func() error { _revlTimer1.Cancel(); return nil }" in src
    # the firing body carries the emission, audited like a top-level emit
    assert 'log.Write("tick")' in src
    # the clock coeffect preamble is present (and deterministic-advance driver)
    assert "func RevlClockAdvance(ms int64) int" in src


def test_no_timer_no_clock_preamble():
    """A component with no timer must not carry the clock preamble (byte-stable
    output for the frozen scenarios)."""
    ir = _load(USER_CACHE)
    src = emit.emit(ir, package="usercache")
    assert "RevlClockAdvance" not in src
    assert "RevlTimer" not in src


ADVANCE = HERE / "scenarios" / "emitted" / "advance" / "advance.ir.json"


def test_advance_lifecycle_step_drives_the_clock():
    """item 102 (go half): a `lifecycle test`'s `advance <n><unit>` step lowers
    to RevlClockAdvance(N), with RevlClockReset() at test start so each test's
    clock is independent. Before this, the go emitter refused `advance` outright
    (`unknown lifecycle step 'advance'`)."""
    src = emit.emit(_load(ADVANCE), package="advance")
    # the advance steps drive the deterministic clock forward (35s / 100s / …)
    assert "RevlClockAdvance(35000)" in src
    assert "RevlClockAdvance(100000)" in src
    # a test that advances resets the package-global clock first
    assert "RevlClockReset()" in src
    # the clock preamble (advance/reset driver) is pulled in
    assert "func RevlClockAdvance(ms int64) int" in src
    assert "func RevlClockReset() {" in src


def test_advance_generated_test_file_is_current():
    """The committed self-running advance test is byte-current with a fresh
    emit (modulo gofmt) — the reproducibility gate regen.sh guarantees."""
    fresh = emit.emit(_load(ADVANCE), package="advance")
    committed = (ADVANCE.parent / "gen_advance_test.go").read_text(encoding="utf-8")
    formatted = _gofmt(fresh)
    if formatted is not None:
        assert formatted == committed, (
            "advance/gen_advance_test.go is stale — run `python3 tools/regen_goldens.py go`")
        return
    norm = lambda s: " ".join(s.split())
    assert norm(fresh) == norm(committed), (
        "advance/gen_advance_test.go is stale — run `python3 tools/regen_goldens.py go`")


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
    # format -> a `+` chain of string-typed pieces, not fmt.Sprintf: `%v`
    # takes ...any and boxes each operand, and the operand types are known at
    # emit time (item 434 (f))
    assert '("INSERT INTO cache_log VALUES (" + key + ")")' in src
    assert "INSERT INTO cache_log VALUES (%v)" not in src


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


def _body(src, name):
    """One emitted function, so an assertion about a lowering cannot be
    satisfied (or defeated) by an identically-spelled line in a runtime helper
    — `revlListPush`'s own body is `out = append(out, x)`."""
    start = src.index(f"func {name}(")
    return src[start:src.index("\n}\n", start)]


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


# A wildcarded payload (`Found(_) => ..`) has no name to bind. The parser
# records that as `bind == "_"` (a real, non-empty string), not `None` (the
# same shape the cordis-rs backend hit — roadmap item 186 / the Rust wildcard-
# payload fix). Before this fix every case-bind site here (the plain type
# switch, and the Opt/Result tuple-match component helpers) treated any
# truthy bind as a real name: it declared `_ := _m.Value` and read it back
# with `_ = _` — a literal `_` used as a value, which Go refuses ("cannot use
# _ as value", "no new variables on left side of :="). The fix discards the
# payload exactly like an unbound arm.
def test_wildcard_payload_arm_discards_rather_than_binds_underscore():
    src = emit.emit(_compile("""
type Outcome = Found(Int) | Missing
fn has(o: Outcome) -> Bool {
  return match o { Found(_) => true, Missing => false }
}
"""))
    _has(src, "switch _m := o.(type) {")
    _has(src, "case OutcomeFound:")
    # the malformed construct this regression guards against, verbatim.
    assert "_ := _m.Value" not in src
    assert "_ = _\n" not in src


def test_v3_stdlib_emit_shapes():
    src = emit.emit(_load(V3_STDLIB), package="stdlib")
    # Opt and Result both two-value structs on the pure v3 tier (item 434 (d))
    _has(src, "type RevlOpt[T any] struct {")
    _has(src, "type RevlResult[T any, E any] struct {")
    assert "interface{ isRevlResult() }" not in src
    assert "interface { isRevlResult() }" not in src
    # optfield/optcall -> revlOptMap over the payload; the record field is
    # exported (item 390: json_stringify(record) needs exported struct fields)
    _has(src, "revlOptMap(row, func(_x Row) string { return _x.Name })")
    _has(src, "revlOptMap(s, func(_x string) int64 { return revlStrCharCodeAt(_x, 0) })")
    # stdlib builtins dispatch Str vs List
    _has(src, "revlStrLen(s)")
    _has(src, "revlListPush(xs, x)")
    # template -> a `+` chain over the inferred operand types (item 434 (f)):
    # a Str part concatenates as-is and an Int part renders through
    # strconv.FormatInt, so nothing is boxed into an `any` and the module needs
    # no `fmt` import at all; arrow -> Go closure; Result construction
    _has(src, '("hi " + name + "#" + strconv.FormatInt(n, 10) + "!")')
    assert '"hi %v#%v!"' not in src
    _has(src, "func(y int64) int64 { return (y + 1) }")
    _has(src, "RevlResult[int64, string]{OkV: n, Ok: true}")


# ---------------------------------------------------------------------------
# Roadmap item 94 — async function-value color erasure (follow-up to item 92).
#
# Item 92 added `Async[T]` (docs/design/async-function-values.md): a first-class
# callback whose declared return is `Async[T]` colors async/await on py/ts. The
# go tier has no async-fn machinery, so it *erases* the color — but the fn-type
# renderer passed the `Async[T]` return through untouched, so
# `(Str) -> Async[Str]` emitted the invalid Go `func(string) Async[Str]` that
# `go build` rejects. The fix erases the async return to its concrete `T`.

def _async_callback_ir():
    from revl import compile_source  # noqa: PLC0415
    return compile_source(
        "fn agent_loop(prompt: Str, complete: (Str) -> Async[Str], "
        "max_steps: Int) -> Str {\n"
        "  let first: Str = complete(prompt)\n"
        "  return first\n"
        "}\n"
    )


def test_async_typed_callback_return_erases_to_concrete_type():
    src = emit.emit(_async_callback_ir(), package="asyncfn")
    # the erased color renders the concrete Go return type, never `Async[T]`
    _has(src, "complete func(string) string")
    assert "Async" not in src


def test_go_build_accepts_async_typed_callback_erasure():
    """Definition-of-done pin: an async-typed callback param emits Go that a
    real `go build` accepts — `func(...) T`, never the invalid `Async[T]`."""
    import sys  # noqa: PLC0415

    sys.path.insert(0, str(ROOT / "tools"))
    from validate import GoValidator  # noqa: PLC0415

    validator = GoValidator()
    reason = validator.unavailable()
    if reason:
        pytest.skip(reason)
    src = emit.emit(_async_callback_ir(), package="asyncfn")
    results = validator.check([("async-fn-value-erasure", src)])
    status, detail = results["async-fn-value-erasure"]
    assert status == "ok", detail


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
            f"{rel} is stale — run `python3 tools/regen_goldens.py go`")
        return
    assert formatted == committed, f"{rel} is stale — run `python3 tools/regen_goldens.py go`"


MEMKV = HERE / "scenarios" / "emitted" / "memkv" / "memkv.ir.json"
TIMER = HERE / "scenarios" / "emitted" / "timer" / "timer.ir.json"
# item 113: the host Map value type — Map[Str, Int] and Map[Str, List[Str]].
COUNTER = HERE / "scenarios" / "emitted" / "counter" / "counter.ir.json"
TAGGER = HERE / "scenarios" / "emitted" / "tagger" / "tagger.ir.json"


@pytest.mark.parametrize("ir_path,pkg", [
    (USER_CACHE, "usercache"),
    (TENANTS, "tenants"),
    (MEMKV, "memkv"),
    (TIMER, "timer"),
    (COUNTER, "counter"),
    (TAGGER, "tagger"),
])
def test_checked_in_generated_is_current(ir_path, pkg):
    """The committed gen.go must match a fresh emit (modulo gofmt)."""
    fresh = emit.emit(_load(ir_path), package=pkg)
    committed = (HERE / "scenarios" / "emitted" / pkg / "gen.go").read_text(encoding="utf-8")
    # gofmt the fresh emit first: gofmt splits the inline `;`-joined statements
    # the `??` closure emits onto their own lines (a token change a bare
    # whitespace-normalize would miss), so compare gofmt-to-gofmt when gofmt is
    # present, and fall back to a whitespace-insensitive check when it is not.
    formatted = _gofmt(fresh)
    if formatted is not None:
        assert formatted == committed, (
            f"{pkg}/gen.go is stale — run `python3 tools/regen_goldens.py go`")
        return
    norm = lambda s: " ".join(s.split())
    assert norm(fresh) == norm(committed), (
        f"{pkg}/gen.go is stale — run `python3 tools/regen_goldens.py go`")


# --- scenarios/emitted freshness gate --------------------------------------
# The checked-in Go under scenarios/emitted/*/ (the `gen.go` component outputs
# AND the `gen_<name>_test.go` lifecycle-test outputs) is regenerated from each
# scenario's IR by backends/go/regen.sh. Nothing gated these against emit.py
# drift before: a change to backends/go/emit.py that touches the shared prelude
# can silently stale a scenario whose regen step was not re-run — exactly how
# item 247's RevlFrame prelude change left the provide_method_witnessed golden
# stale with no failing test. test_v3_checked_in_generated_is_current covers
# only the v3/ fixtures, so this parametrized twin closes the gap for the
# scenarios/emitted goldens. The table mirrors regen.sh's scenarios/emitted
# stanzas one-to-one; each row pins (id, ir_path, package, output_rel_path).
_EMITTED = HERE / "scenarios" / "emitted"
SCENARIO_GOLDENS = [
    ("usercache", USER_CACHE, "usercache", "usercache/gen.go"),
    ("tenants", TENANTS, "tenants", "tenants/gen.go"),
    ("memkv", _EMITTED / "memkv" / "memkv.ir.json", "memkv", "memkv/gen.go"),
    ("counter", _EMITTED / "counter" / "counter.ir.json", "counter", "counter/gen.go"),
    ("tagger", _EMITTED / "tagger" / "tagger.ir.json", "tagger", "tagger/gen.go"),
    ("spawn", _EMITTED / "spawn" / "spawn.ir.json", "spawn", "spawn/gen.go"),
    ("accessor", _EMITTED / "accessor" / "accessor.ir.json", "accessor", "accessor/gen.go"),
    ("timer", _EMITTED / "timer" / "timer.ir.json", "timer", "timer/gen.go"),
    ("advance", _EMITTED / "advance" / "advance.ir.json", "advance",
     "advance/gen_advance_test.go"),
    ("records", _EMITTED / "records" / "records.ir.json", "records",
     "records/gen_records_test.go"),
    ("jsonwire", _EMITTED / "jsonwire" / "jsonwire.ir.json", "jsonwire",
     "jsonwire/gen_jsonwire_test.go"),
    ("witnessed_teardown", _EMITTED / "witnessed_teardown" / "witnessed_teardown.ir.json",
     "witnessedteardown", "witnessed_teardown/gen_witnessed_teardown_test.go"),
    ("provide_method_witnessed",
     _EMITTED / "provide_method_witnessed" / "provide_method_witnessed.ir.json",
     "providemethodwitnessed",
     "provide_method_witnessed/gen_provide_method_witnessed_test.go"),
    ("method_compensate", _EMITTED / "method_compensate" / "method_compensate.ir.json",
     "methodcompensate", "method_compensate/gen_method_compensate_test.go"),
]


@pytest.mark.parametrize(
    "ir_path,pkg,rel",
    [(ir, pkg, rel) for _id, ir, pkg, rel in SCENARIO_GOLDENS],
    ids=[g[0] for g in SCENARIO_GOLDENS],
)
def test_scenario_checked_in_generated_is_current(ir_path, pkg, rel):
    """Each scenarios/emitted golden must be byte-identical to a fresh
    gofmt(emit(ir)) — the drift gate regen.sh guarantees. Parametrized per
    scenario so a failure names the exact stale golden."""
    fresh = emit.emit(_load(ir_path), package=pkg)
    committed = (_EMITTED / rel).read_text(encoding="utf-8")
    formatted = _gofmt(fresh)
    if formatted is not None:  # gofmt present: compare gofmt-to-committed
        assert formatted == committed, (
            f"scenarios/emitted/{rel} is stale — run `python3 tools/regen_goldens.py go`")
        return
    # no gofmt: fall back to a whitespace-insensitive check
    assert _ws(fresh) == _ws(committed), (
        f"scenarios/emitted/{rel} is stale — run `python3 tools/regen_goldens.py go`")


# host `Map.new()` iteration surface — `keys()` / `size()` (roadmap item 88).
# The value-Map builtins `size()`/`keys()` (docs/stdlib-2.0.md §Map) type-check
# on a host `Map.new()` receiver too (host-receiver provenance isn't tracked in
# provide bodies), and emit lowers both as plain method calls on the runtime
# object. The host `type Map struct` therefore has to carry those methods, or
# the emitted component fails `go build` (`s.store.Size undefined`). The
# executable proof — running keys()/size() on real stc-go — lives in
# scenarios/emitted/memkv/gen_exec_test.go; this pins the emitted shapes.
# Mirrors the ts/rust/java gates (item 86) and the python fix (item 84).
_HOST_MAP_ITER_SRC = """
service KV {
  fn count() -> Int
  fn all_keys() -> List[Str]
  emission fn put(key: Str, value: Str)
}

component MemKV provides kv: KV {
  let store = effect Map.new() undo store.drop()

  provide kv {
    fn count()    = store.size()
    fn all_keys() = store.keys()
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""


def test_host_map_backs_keys_and_size():
    """The host `type Map struct` runtime carries `Size`/`Keys`, and the provide
    body lowers them as method calls on the store — with value-Map semantics:
    `Size` is the entry count as the tier's revl Int (a Go `int`, matching the
    emitted `Count() int` service signature), `Keys` sorts into canonical order."""
    src = emit.emit(_compile(_HOST_MAP_ITER_SRC), package="memkv")
    # runtime methods exist on the host Map object (item 113: the host Map is
    # generic over its value type, `Map[V]`, so the methods carry the receiver
    # type parameter; Size is the tier's revl Int — a Go `int` in v1/v2 mode)
    assert "func (m *Map[V]) Size() int {" in src
    assert "func (m *Map[V]) Keys() []string {" in src
    # canonical (code-point) order: go's `string <` is UTF-8 byte lexicographic,
    # exactly code-point order, and slices.Sort on []string orders by `<`: the
    # same order the hand-rolled insertion sort produced, in O(n log n)
    # (item 434 (h); keys() is the Map iteration surface, so it is on the path
    # of every map traversal, not just small symbol tables)
    assert "\tslices.Sort(ks)" in src
    assert "for j := i; j > 0 && ks[j] < ks[j-1]; j--" not in src
    # provide body lowers to method calls on the host object
    assert "return revlSelf.store.Size()" in src
    assert "return revlSelf.store.Keys()" in src
    # read-only queries leave no host trace (no hostRecord in Size/Keys)
    body = src.split("func (m *Map[V]) Size()")[1].split("func (m *Map[V]) Keys()")[0]
    assert "hostRecord" not in body


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


def test_map_lookup_answers_the_opt_struct_and_has_bool():
    src = emit.emit(_compile(
        'pub fn get(m: Map[Str, Int], k: Str) -> Int { return m.lookup(k) ?? 0 }\n'
        'pub fn member(m: Map[Str, Int], k: Str) -> Bool { return m.has(k) }\n'))
    assert "revlMapGet(m, k)" in src and "revlMapHas(m, k)" in src
    # lookup answers a RevlOpt, so the Opt preamble is pulled in for `??`
    assert "type RevlOpt[T any] struct {" in src
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


# ---- host Map value type (item 113, FR-4) ----------------------------------
# The host `Map.new()` object is generic over its value type, so a `Map[Str, Int]`
# counter or `Map[Str, List[Str]]` ledger lowers Insert/Get against the DECLARED
# value type — not a hardcoded String. Go cannot infer `V` from the argument-less
# `MapNew()`, so emit pins it at the acquisition (`MapNew[int]()`). Before this,
# a component that inserted a non-String value failed `go build`
# (`cannot use n (int) as string value`). Mirrors backends/rust/emit.py's
# `struct Map<V>` (FR-4). The executable proofs live in
# scenarios/emitted/{counter,tagger}/gen_exec_test.go.

_HOST_MAP_INT_SRC = """
service Tally {
  fn total() -> Int
  fn get(key: Str) -> Int
  emission fn bump(key: Str, amount: Int)
}

component Counter provides tally: Tally {
  let store = effect Map.new() undo store.drop()

  provide tally {
    fn total()   = store.size()
    fn get(key)  = store.get(key) ?? 0
    fn bump(key, amount) {
      effect store.insert(key, amount)
      undo   store.remove(key)
    }
  }
}
"""


def test_host_map_is_generic_over_its_value_type():
    """The host Map runtime is `Map[V any]`, and a `Map[Str, Int]` binding pins
    V from its `insert` sites so Insert/Get carry Int values (not String)."""
    src = emit.emit(_compile(_HOST_MAP_INT_SRC), package="counter")
    # generic runtime type + value-typed methods
    assert "type Map[V any] struct {" in src
    assert "func MapNew[V any]() *Map[V] {" in src
    assert "func (m *Map[V]) Insert(k string, v V) {" in src
    assert "func (m *Map[V]) Get(k string) (V, bool) {" in src
    # the store binding pins V = int (a v1/v2 component: Int is Go `int`), so
    # every reference site instantiates Map[int] consistently
    assert "var store *Map[int]" in src
    assert "store = MapNew[int]()" in src
    assert "store *Map[int]" in src  # the impl struct field
    # the Int value crosses Insert unchanged — the round-trip that a String-only
    # host Map could not compile
    assert "revlSelf.store.Insert(key, amount)" in src


def test_host_map_pins_a_compound_list_value_type():
    """A `Map[Str, List[Str]]` binding pins V = []string from an `insert` whose
    value is a List[Str] param (learned across the provide methods)."""
    src = emit.emit(_compile("""
service Groups {
  fn get_or(key: Str, fallback: List[Str]) -> List[Str]
  emission fn set(key: Str, tags: List[Str])
}

component Tagger provides g: Groups {
  let store = effect Map.new() undo store.drop()

  provide g {
    fn get_or(key, fallback) = store.get(key) ?? fallback
    fn set(key, tags) {
      effect store.insert(key, tags)
      undo   store.remove(key)
    }
  }
}
"""), package="tagger")
    assert "var store *Map[[]string]" in src
    assert "store = MapNew[[]string]()" in src


def test_host_map_defaults_to_string_when_no_insert_pins_a_type():
    """A read-only / write-free host Map keeps the historical String surface —
    memkv's `Map[Str, Str]` still emits Map[string] (byte-identical intent)."""
    src = emit.emit(_compile(_HOST_MAP_ITER_SRC), package="memkv")
    assert "var store *Map[string]" in src
    assert "store = MapNew[string]()" in src


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


_V3_RECORD_PROVIDE_SRC = """
type Person = { name: Str, age: Int }

service Maker {
  fn make(n: Str, a: Int) -> Person
  fn older(p: Person) -> Person
}

component Factory provides maker: Maker {
  provide maker {
    fn make(n, a) = { name: n, age: a }
    fn older(p) = { name: p.name, age: p.age + 1 }
  }
}

component User requires maker: Maker {
}

lifecycle test "record round-trips through a provide method" {
  load Factory
  let p = call maker.make("ada", 36)
  assert p.age == 36
  let q = call maker.older(p)
  assert q.age == 37
  assert q.name == "ada"
  unload Factory
  assert no_residue
}
"""


def test_v3_record_lowers_in_a_live_provide_method():
    """item 139: a `provide` method that TAKES and RETURNS a record lowers in
    emit()'s live stc-go component world (kept on that path by a lifecycle test
    driving it). Before this the component-world renderer carried no v3
    record/ADT lowering and refused — even though the same record lowers fine
    in a top-level fn and on the placement runner. The declared record is
    materialized as a Go struct in the same package (exported, json-tagged) and
    the method signatures + literal + field access lower against it."""
    src = emit.emit(_compile(_V3_RECORD_PROVIDE_SRC), package="factory")
    # the record type is materialized in the live module
    _has(src, "type Person struct {")
    _has(src, 'Name string `json:"name"`')
    _has(src, 'Age int64 `json:"age"`')
    # record param + record return in the provide-impl method signatures
    _has(src, "func (revlSelf *Factory_maker) Make(n string, a int64) Person")
    _has(src, "func (revlSelf *Factory_maker) Older(p Person) Person")
    # a record literal and a field access lower inside the method bodies
    _has(src, "return Person{Name: n, Age: a}")
    _has(src, "return Person{Name: p.Name, Age: (p.Age + 1)}")


def test_v3_provide_method_param_named_s_does_not_collide_with_receiver():
    """item 147: the provide-impl method receiver is `revlSelf` (a reserved,
    revl-prefixed name), not the bare `s` it used to hardcode — so a provide
    method with a parameter named `s` no longer emits `func (s *C_k) M(s T)`,
    which Go rejects as "s redeclared in this block". Affects v1/v2/v3 alike."""
    src = emit.emit(_compile("""
        service Shaper { fn area(s: Int) -> Int }
        component C provides shaper: Shaper {
          provide shaper {
            fn area(s) = s * s
          }
        }
    """), package="shaper")
    _has(src, "func (revlSelf *C_shaper) Area(s int) int")
    _has(src, "return (s * s)")
    # the receiver name never leaks as a bare `func (s *` anywhere
    assert "func (s *" not in src


def test_v3_record_in_component_without_types_still_refuses():
    """The refusal path is intact where it must be: a component that spells a
    record literal but declares NO record type (v1/v2 shape, or a v3 doc with
    no `type`) has nothing to lower against, so the named tier limit still
    fires rather than mis-emitting."""
    ir = _load(USER_CACHE)  # a v1/v2 component document, no declared types
    src = emit.emit(ir, package="usercache")
    # byte-stable: no v3 record scaffolding leaks into a v1/v2 module
    assert "json:" not in src
    assert "_V3" not in src


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


# ---------------------------------------------------------------------------
# extern @go bodies: `//revl:import` hoist + revl `Any` -> Go `any` (item 140).
# A verbatim extern body cannot spell its own `import`, so the @go body places
# a `//revl:import <path>` directive that the emitter lifts to the module's
# import block and drops from the function body.

def _extern_ir(go_body: str, *, returns="Any", params=None) -> dict:
    return {
        "ir_version": 3, "types": {}, "functions": [],
        "externs": [{
            "name": "probe", "class": "pure",
            "params": params or [{"name": "s", "type": "Str"}],
            "returns": returns, "bodies": {"go": go_body},
        }],
        "tests": [],
    }


def test_extern_go_body_hoists_revl_import_directive():
    ir = _extern_ir(
        "//revl:import encoding/json\n"
        "var v any\n"
        "_ = json.Unmarshal([]byte(s), &v)\n"
        "return v"
    )
    src = emit.emit(ir, package="p")
    # the package is hoisted into the module import block...
    assert '"encoding/json"' in src
    assert "import (" in src
    # ...and the directive line itself never survives in the emitted body
    assert "//revl:import" not in src
    assert "json.Unmarshal([]byte(s), &v)" in src


def test_extern_go_body_hoists_quoted_import_path():
    # the path may be written quoted, exactly as Go spells an import
    src = emit.emit(_extern_ir('//revl:import "encoding/json"\nreturn nil',
                               returns="Any"), package="p")
    assert '"encoding/json"' in src
    assert '""encoding/json""' not in src  # not double-quoted


def test_revl_any_maps_to_go_any():
    src = emit.emit(_extern_ir("return s", returns="Any"), package="p")
    assert "func probe(s string) any {" in src


def test_jsonwire_scenario_ir_emits_encoding_json_backed_bodies():
    """The checked-in item-140 proof IR emits the two JSON bodies with the
    hoisted `encoding/json` import (the executable proof runs under `go test`
    in scenarios/emitted/jsonwire/)."""
    ir = _load(HERE / "scenarios" / "emitted" / "jsonwire" / "jsonwire.ir.json")
    src = emit.emit(ir, package="jsonwire")
    assert "func json_parse(s string) any {" in src
    assert "func json_stringify(v any) string {" in src
    assert "json.Unmarshal([]byte(s), &v)" in src
    assert "json.Marshal(v)" in src
    assert '"encoding/json"' in src


# ---------------------------------------------------------------------------
# Roadmap item 434 (a)/(b): the self-rebind (unique-ownership) analysis.
#
# `out = out.push(x)` and `m = m.set(k, v)` copy through revlListPush /
# revlMapSet, which is the correct lowering for a persistent value and a
# quadratic for the loop idiom (measured: 1001 allocs / 4,274,103 B to build a
# 1000-element list, against a hand-written `append`'s 1 / 8,192). The
# destructive lowering is sound only where nothing can observe the write, so
# these tests pin BOTH halves: the idiom is rewritten, and every shape that
# could alias the old value keeps the copying helper.


def test_self_rebind_list_and_map_lower_in_place():
    src = emit.emit(_compile("""
fn collect(n: Int) -> List[Int] {
  var out: List[Int] = []
  var i = 0
  while (i < n) {
    out = out.push(i)
    i += 1
  }
  return out
}
fn tally(words: List[Str]) -> Int {
  var m: Map[Str, Int] = Map.empty()
  for (w of words) { m = m.set(w, 1) }
  return m.size()
}
fn drop(words: List[Str]) -> Int {
  var m: Map[Str, Int] = Map.empty()
  for (w of words) { m = m.set(w, 1) }
  for (w of words) { m = m.remove(w) }
  return m.size()
}
"""))
    _has(src, "out = append(out, i)")
    _has(src, "m[w] = 1")
    _has(src, "delete(m, w)")
    assert "revlListPush(out, i)" not in src
    assert "revlMapSet(m, w, 1)" not in src
    assert "revlMapRemove(m, w)" not in src


def test_self_rebind_refused_when_the_old_value_can_be_observed():
    """Each function here writes the binding exactly as `collect` does, and
    each adds one way for the previous value to survive the rebind. The write
    THAT ESCAPE CAN REACH may not lower in place: `append` would write into a
    slice a second name still holds.

    Item 445 made the question per-write instead of per-name, so the write
    BEFORE each escape is still owned and still lowers in place — nothing holds
    the value yet at that point. The copying `revlListPush` then rebinds `out`
    to a brand-new slice, which is why the escape does not leak past it."""
    src = emit.emit(_compile("""
fn size_of(xs: List[Int]) -> Int { return xs.length() }
fn aliased(n: Int) -> List[Int] {
  var out: List[Int] = []
  out = out.push(n)
  var snap = out
  out = out.push(n)
  return snap
}
fn nested(n: Int) -> List[List[Int]] {
  var out: List[Int] = []
  var rows: List[List[Int]] = []
  out = out.push(n)
  rows = rows.push(out)
  out = out.push(n)
  return rows
}
fn loopwise(xs: List[Int]) -> List[List[Int]] {
  var out: List[Int] = []
  var rows: List[List[Int]] = []
  for (x of xs) {
    rows = rows.push(out)
    out = out.push(x)
  }
  return rows
}
"""))
    # one refused write per function: the one the escape above it reaches
    assert _body(src, "aliased").count("out = revlListPush(out, n)") == 1
    assert _body(src, "aliased").count("out = append(out, n)") == 1
    assert _body(src, "nested").count("out = revlListPush(out, n)") == 1
    assert _body(src, "nested").count("out = append(out, n)") == 1
    # the escape is a loop-carried one in `loopwise`: the fixpoint over the back
    # edge carries `rows = rows.push(out)` round to the push below it, so unlike
    # the straight-line pairs above there is no owned write left in that body
    assert "out = append(out, x)" not in _body(src, "loopwise")
    # `rows` is written from `rows.push(out)`, a self-rebind, but `out` is an
    # ARGUMENT there, which is what disqualifies `out`, not `rows`. `rows` is
    # never aliased, so it still lowers in place.
    _has(src, "rows = append(rows, out)")


def test_a_retaining_call_disqualifies_the_write_after_it():
    """The rule closes over calls, but on the whole-program summary item 445 (b)
    added rather than on "every call retains". `echo` hands its parameter back,
    so the caller's next `append` would be visible through what came back and
    the copying helper stays. `size_of` walks its argument and answers an Int,
    so it keeps nothing and the write is still owned — which is what takes
    `stdlib/list.rvl`'s `list_dedup` off its emitted quadratic.

    A `slice` header is exactly the alias this is protecting: `append` into a
    slice a second name still holds writes through both."""
    src = emit.emit(_compile("""
fn size_of(xs: List[Int]) -> Int { return xs.length() }
fn echo(xs: List[Int]) -> List[Int] { return xs }
fn measured(xs: List[Int]) -> Int {
  var out: List[Int] = []
  var seen = 0
  for (x of xs) {
    seen = seen + size_of(out)
    out = out.push(x)
  }
  return seen
}
fn handed_back(xs: List[Int]) -> Int {
  var out: List[Int] = []
  var snap: List[Int] = []
  for (x of xs) {
    snap = echo(out)
    out = out.push(x)
  }
  return snap.length()
}
"""))
    assert "out = append(out, x)" in _body(src, "measured")
    assert "revlListPush" not in _body(src, "measured")
    assert "out = revlListPush(out, x)" in _body(src, "handed_back")
    assert "out = append(out, x)" not in _body(src, "handed_back")


# ---------------------------------------------------------------------------
# Roadmap item 434 (e): the string-accumulator lowering.
#
# `out = out + x + sep` in a loop allocates one whole intermediate string per
# iteration (1001 allocs / 3,717,392 B at n=1000, against a strings.Builder's
# 1 / 8,192). A Go string cannot be mutated, so unlike (a)/(b) the fix is not a
# destructive rebind but a Builder that spans the loop. It applies only when
# nothing can observe the accumulator between the loop's first and last write.


def test_str_accumulator_loop_uses_a_builder():
    src = emit.emit(_compile("""
fn build(xs: List[Str], sep: Str) -> Str {
  var out = ""
  for (x of xs) { out = out + x + sep }
  return out
}
"""))
    _has(src, "var _revlSB0 strings.Builder")
    _has(src, "_revlSB0.WriteString(out)")
    _has(src, "_revlSB0.WriteString(x)")
    _has(src, "_revlSB0.WriteString(sep)")
    _has(src, "out = _revlSB0.String()")
    assert "out = (out + x)" not in src


def test_str_accumulator_refused_when_the_partial_value_is_read():
    """A read of the accumulator inside the loop, and a `return` out of it,
    each need the partial string to exist on every iteration; the Builder only
    materializes after the loop, so neither may be rewritten."""
    src = emit.emit(_compile("""
fn counted(xs: List[Str]) -> Int {
  var out = ""
  var n = 0
  for (x of xs) {
    out = out + x
    n = n + out.length()
  }
  return n
}
fn early(xs: List[Str]) -> Str {
  var out = ""
  for (x of xs) {
    out = out + x
    if (out.length() > 8) { return out }
  }
  return out
}
"""))
    assert "strings.Builder" not in src
    assert src.count("out = (out + x)") == 2


def test_str_accumulator_refused_when_the_loop_condition_reads_it():
    """The subtle one: the read is in the `while` condition, OUTSIDE the body,
    and it runs before every iteration. The occurrence scan therefore covers
    the whole loop node, not just its body."""
    src = emit.emit(_compile("""
fn capped(xs: List[Str]) -> Str {
  var out = ""
  var i = 0
  while (out.length() < 10 && i < xs.length()) {
    out = out + xs[i]
    i += 1
  }
  return out
}
"""))
    assert "strings.Builder" not in src
    _has(src, "out = (out + xs[i])")


# ---------------------------------------------------------------------------
# Roadmap item 434 (c) stage two: the code-point scan lowering.
#
# Stage one made ONE `s.charCodeAt(i)` allocation-free, but each read still
# walks to index i, so the scan idiom stayed quadratic in time: the harness
# measures the emitted 78-code-point scan at ~9.9 us and the 780-code-point one
# at ~853 us, an 86x cost for a 10x input, against a hand-written
# `for _, r := range s` growing 10.5x. Stage two recognises the loop shape and
# emits `range`, which is the same loop and hands the rune over. The rewrite is
# equivalent only for the exact shape, so these tests pin both halves.


def test_scan_loop_lowers_to_range_over_the_string():
    src = emit.emit(_compile("""
fn scan(s: Str) -> Int {
  var i = 0
  var acc = 0
  var n = s.length()
  while (i < n) {
    acc += s.charCodeAt(i)
    i += 1
  }
  return acc
}
fn first_at(s: Str, c: Int) -> Int {
  var i = 0
  while (i < s.length()) {
    if (s.charAt(i) == "x") { return i }
    if (s.codepoint_at(i) == c) { return i }
    i += 1
  }
  return 0 - 1
}
"""))
    _has(src, "for _, _revlR0 := range s {")
    _has(src, "acc = revlAdd(acc, int64(_revlR0))")
    # the bound may be written inline; `range` runs exactly revlStrLen(s) times
    _has(src, 'string(_revlR0) == "x"')
    _has(src, "int64(_revlR0) == c")
    assert "revlStrCharCodeAt(s, i)" not in src
    assert "revlStrCharAt(s, i)" not in src


def test_scan_loop_refused_for_every_shape_that_is_not_a_scan():
    """Each function reads a code point at an index in a `while`, and each
    breaks one of the facts the `range` rewrite rests on: the index must be 0
    on entry, must advance by exactly 1 per iteration at the end of the body,
    must not be skipped by a `continue`, and the bound must be the length of
    the string being read. Break any one and the loop keeps the helper."""
    src = emit.emit(_compile("""
fn step2(s: Str) -> Int {
  var i = 0
  var acc = 0
  var n = s.length()
  while (i < n) {
    acc += s.charCodeAt(i)
    i += 2
  }
  return acc
}
fn from_one(s: Str) -> Int {
  var i = 1
  var acc = 0
  var n = s.length()
  while (i < n) {
    acc += s.charCodeAt(i)
    i += 1
  }
  return acc
}
fn skipping(s: Str) -> Int {
  var i = 0
  var acc = 0
  var n = s.length()
  while (i < n) {
    if (s.charCodeAt(i) == 32) { i += 1; continue }
    acc += s.charCodeAt(i)
    i += 1
  }
  return acc
}
fn other_bound(s: Str, t: Str) -> Int {
  var i = 0
  var acc = 0
  var n = t.length()
  while (i < n) {
    acc += s.charCodeAt(i)
    i += 1
  }
  return acc
}
fn rebound(t: Str, u: Str) -> Int {
  var s = t
  var i = 0
  var acc = 0
  var n = s.length()
  while (i < n) {
    acc += s.charCodeAt(i)
    s = u
    i += 1
  }
  return acc
}
"""))
    assert "range s {" not in src
    assert src.count("revlStrCharCodeAt(s, i)") == 6


# ---------------------------------------------------------------------------
# Roadmap item 434 (g): Str.indexOf / Str.slice / Str.split without []rune.


def test_str_helpers_do_not_materialize_the_whole_string():
    src = emit.emit(_compile(
        'pub fn probe(s: Str, sub: Str) -> Int { return s.indexOf(sub) }\n'
        'pub fn take(s: Str, a: Int, b: Int) -> Str { return s.slice(a, b) }\n'
        'pub fn chars(s: Str) -> List[Str] { return s.split("") }\n'))
    # indexOf: the standard library's search plus one rune count over the
    # matched prefix, instead of two []rune copies and a naive O(n*m) scan
    _has(src, "\tb := strings.Index(s, sub)")
    _has(src, "\t\treturn int64(utf8.RuneCountInString(s[:b]))")
    # slice: a byte walk returning a substring, which shares s's bytes
    _has(src, "\tlo, hi, i := len(s), len(s), int64(0)")
    _has(src, "\treturn s[lo:hi]")
    # split(""): substrings, not one fresh string per code point
    _has(src, "\t\t\t\tout = append(out, s[off:off+w])")
    # the []rune forms survive only as the invalid-UTF-8 fallbacks, where
    # `[]rune` substitutes U+FFFD and a byte walk cannot
    _has(src, "\t\t\treturn string([]rune(s)[a:b])")
    _has(src, "\t\trs := []rune(s)")
    assert "[]rune" not in src.split("func revlStrSplit")[1].split("\n}")[0]
