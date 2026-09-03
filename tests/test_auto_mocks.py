"""Auto-mocks: every `service` declaration ships a free fake (roadmap item 60).

`revl test --mock-requires` runs every `lifecycle test` in mock world: each
unmet `requires` a loaded component leaves is filled by a generated in-memory
provider whose responses come from the item-37 generators (`src/revl/fault.py`)
— typed, seeded, deterministic; an `emission` operation is recorded-not-crossed
(the mock counts the boundary crossing, never makes it), and the report says
exactly what would have been emitted. Layers, gated separately so a missing
runtime never looks like a pass:

* **synthesis** — `gen_value` / `_make_operation` / `MockRecorder` /
  `make_mock_component` unit behavior (typedness, seeding, fallbacks, report
  shape). No runtime needed; always run.
* **wiring** — the `--mock-requires` CLI paths: skip-with-a-reason when cordis
  is absent, no-op with a reason when the document has no lifecycle test, the
  base-module emission regression (`_revl_i64` & friends must survive).
  No runtime needed; always run.
* **execution** — real compositions booted in mock world on a real
  `cordis.Context` (stratum-3 unit testing). Skipped with a reason when
  cordis-py is absent (`sh backends/python/setup.sh`).
* **CLI** — the whole `revl test --mock-requires` subprocess under the
  backend's own venv (the one with cordis-py installed).
"""

import importlib.util
import random
import subprocess
import sys
import types as _types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import mocks as mocks_mod  # noqa: E402
from revl.test import test_command as _test_command  # noqa: E402

CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

_HAS_CORDIS = importlib.util.find_spec("cordis") is not None
needs_cordis = pytest.mark.skipif(
    not _HAS_CORDIS,
    reason="mock world activates components for real; it needs the cordis-py "
           "runtime (sh backends/python/setup.sh)")
needs_venv = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _emitter(tier: str):
    spec = importlib.util.spec_from_file_location(
        f"revl_mocks_{tier}_emit", ROOT / "backends" / tier / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# shared sources
# ----------------------------------------------------------------------------
#
# `App` requires `db: Database` and provides `api: Api`; nothing in the
# document provides `db`, so every mock-world boot has zero real providers.

DB_APP = '''
service Database {
  fn ping() -> Bool
  emission fn execute(sql: Str) -> Int
}

service Api {
  emission fn query(sql: Str) -> Int
}

component App requires db: Database provides api: Api {
  provide api {
    fn query(sql) {
      emit db.execute(sql)
      return 7
    }
  }
}

lifecycle test "app runs in mock world" {
  load App
  let n = call api.query("select 1")
  assert n == 7
  unload App
  assert no_residue
}
'''

SHAPES = '''
service KV {
  fn count() -> Int
  fn label() -> Str
  fn alive() -> Bool
  fn rate() -> Float
  fn all() -> List[Str]
  fn get(k: Str) -> Opt[Str]
}

service Api {
  fn count() -> Int
  fn label() -> Str
  fn alive() -> Bool
  fn rate() -> Float
  fn items() -> List[Str]
  fn look(k: Str) -> Opt[Str]
}

component App requires kv: KV provides api: Api {
  provide api {
    fn count() { return kv.count() }
    fn label() { return kv.label() }
    fn alive() { return kv.alive() }
    fn rate() { return kv.rate() }
    fn items() { return kv.all() }
    fn look(k) { return kv.get(k) }
  }
}

lifecycle test "mock values keep their declared shapes" {
  load App
  let n = call api.count()
  assert n - n == 0
  let s = call api.label()
  assert (s + "x") != s
  let b = call api.alive()
  assert (b == true) == b
  let r = call api.rate()
  assert r - r == 0.0
  let items = call api.items()
  let g = call api.look("k")
  unload App
  assert no_residue
}
'''

PRECEDENCE = '''
service KV {
  fn count() -> Int
}

service Api {
  fn count() -> Int
  fn name() -> Str
}

component RealKV provides kv: KV {
  provide kv {
    fn count() { return 99 }
  }
}

component App requires kv: KV provides api: Api {
  provide api {
    fn count() { return kv.count() }
    fn name() { return "app" }
  }
}

lifecycle test "real provider wins over the mock" {
  load RealKV
  load App
  let n = call api.count()
  assert n == 99
  unload App
  unload RealKV
  assert no_residue
}

lifecycle test "mock fills requires with zero setup" {
  load App
  let s = call api.name()
  assert s == "app"
  unload App
  assert no_residue
}
'''

FAILING = '''
service KV {
  fn count() -> Int
}

service Api {
  fn count() -> Int
}

component App requires kv: KV provides api: Api {
  provide api {
    fn count() { return kv.count() }
  }
}

lifecycle test "a failing assertion fails" {
  load App
  let n = call api.count()
  assert n == 12345
  unload App
  assert no_residue
}
'''

RESIDUE = '''
service KV {
  fn count() -> Int
}

service Api {
  fn count() -> Int
}

component App requires kv: KV provides api: Api {
  provide api {
    fn count() { return kv.count() }
  }
}

lifecycle test "leaving a mock loaded is residue" {
  load App
  let n = call api.count()
  assert n - n == 0
  assert no_residue
}
'''

SHARED = '''
service KV {
  fn count() -> Int
}

service A { fn a() -> Int }
service B { fn b() -> Int }

component ConsumerA requires kv: KV provides a: A {
  provide a {
    fn a() { return kv.count() }
  }
}

component ConsumerB requires kv: KV provides b: B {
  provide b {
    fn b() { return kv.count() }
  }
}

lifecycle test "two consumers share one mock" {
  load ConsumerA
  load ConsumerB
  let n = call a.a()
  let m = call b.b()
  assert n - n == 0
  assert m - m == 0
  unload ConsumerA
  unload ConsumerB
  assert no_residue
}

lifecycle test "a consumer can reload against a fresh mock" {
  load ConsumerA
  unload ConsumerA
  load ConsumerA
  let n = call a.a()
  assert n - n == 0
  unload ConsumerA
  assert no_residue
}
'''

RECORDS = '''
type Row = { id: Int, name: Str }
type Outcome = Found(Row) | Missing(Str)

service Store {
  fn row() -> Row
  fn find(k: Str) -> Outcome
  fn bogus(k: Str) -> List[Row]
}

service Api {
  fn row() -> Row
  fn find(k: Str) -> Outcome
  fn bogus(k: Str) -> List[Row]
}

component App requires store: Store provides api: Api {
  provide api {
    fn row() { return store.row() }
    fn find(k) { return store.find(k) }
    fn bogus(k) { return store.bogus(k) }
  }
}

lifecycle test "record and ADT responses are constructed" {
  load App
  let r = call api.row()
  let o = call api.find("k")
  let l = call api.bogus("k")
  unload App
  assert no_residue
}
'''

MIXED = '''
service KV { fn count() -> Int }

service Api { fn count() -> Int }

component App requires kv: KV provides api: Api {
  provide api {
    fn count() { return kv.count() }
  }
}

test "a pure test is not part of mock world" {
  assert true == true
}

lifecycle test "the lifecycle test runs in mock world" {
  load App
  let n = call api.count()
  assert n - n == 0
  unload App
  assert no_residue
}
'''


# ----------------------------------------------------------------------------
# synthesis — gen_value / _make_operation / MockRecorder (no runtime needed)
# ----------------------------------------------------------------------------


def _rng(seed: str = "revl-test"):
    return random.Random(seed)


def test_gen_value_primitives_are_typed():
    """Each declared primitive shape comes back as a value of that shape."""
    cases = [
        ("Int", int), ("Int32", int), ("Bool", bool), ("Str", str),
        ("Float", float), ("F64", float), ("Num", float),
    ]
    for type_str, cls in cases:
        value = mocks_mod.gen_value(type_str, {}, _types.SimpleNamespace(),
                                    _rng())
        assert isinstance(value, cls), f"{type_str} -> {value!r}"


def test_gen_value_container_shapes():
    namespace = _types.SimpleNamespace()
    for type_str, check in [
        ("List[Str]", lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)),
        ("Opt[Int]", lambda v: v is None or isinstance(v, int)),
        ("Map[Str, Int]", lambda v: isinstance(v, dict)),
        ("Unit", lambda v: v is None),
        (None, lambda v: v is None),
    ]:
        value = mocks_mod.gen_value(type_str, {}, namespace, _rng())
        assert check(value), f"{type_str} -> {value!r}"


def test_gen_value_is_seeded_deterministic():
    """The same seed reproduces the same value; a different seed is free to
    differ — reproducibility is the contract, not variety."""
    a = mocks_mod.gen_value("Int", {}, _types.SimpleNamespace(), _rng("seed-a"))
    b = mocks_mod.gen_value("Int", {}, _types.SimpleNamespace(), _rng("seed-a"))
    assert a == b
    c = mocks_mod.gen_value("Str", {}, _types.SimpleNamespace(), _rng("seed-c"))
    d = mocks_mod.gen_value("Str", {}, _types.SimpleNamespace(), _rng("seed-c"))
    assert c == d


def test_gen_value_generator_miss_falls_back_never_raises():
    """A return type the item-37 generator cannot synthesize must not crash a
    mock — it falls back to the structural zero of the declared shape."""
    namespace = _types.SimpleNamespace()
    assert mocks_mod.gen_value("UndeclaredRecord", {}, namespace, _rng()) is None
    assert mocks_mod.gen_value("List[UndeclaredRecord]", {}, namespace, _rng()) == []
    assert mocks_mod.gen_value("Map[UndeclaredRecord, Int]", {}, namespace, _rng()) == {}
    assert mocks_mod.gen_value("Result[UndeclaredRecord, Str]", {}, namespace, _rng()) is None


def test_gen_value_record_is_the_dict_the_emitted_module_reads():
    """A record return type produces a record VALUE, which is a dict.

    A mocked response is handed straight to emitted code, so it has to be in
    the representation emitted code reads: a plain dict keyed by the revl field
    name (`backends/python/emit.py::_emit_types`, docs/records.md §7). This
    asserted `isinstance(value, Row)` instead, so the mock fed the composition
    a shape no emitted module produces — and one the emitter cannot even read
    when a field name collides with a Python keyword, since the class attribute
    is `_mangle`d (`from` -> `from_`) while the read is `v['from']`.
    """
    class Row:                      # the emitted SHAPE class: annotations only
        id: int
        name: str

    types = {"Row": {"kind": "record", "fields": {"id": "Int", "name": "Str"}}}
    module = _types.SimpleNamespace(Row=Row)
    for _ in range(5):
        value = mocks_mod.gen_value("Row", types, module, _rng())
        assert isinstance(value, dict)
        assert set(value) == {"id", "name"}
        assert isinstance(value["id"], int)
        assert isinstance(value["name"], str)


def test_gen_value_record_reads_back_through_the_emitted_field_read():
    """The generated record answers the exact expression the emitter writes,
    keyword-named fields included — the property `isinstance` could not state."""
    types = {"Q": {"kind": "record", "fields": {"from": "Str", "class": "Int"}}}
    module = _types.SimpleNamespace()
    value = mocks_mod.gen_value("Q", types, module, _rng())
    # verbatim `_field_read(target, name)` with the walrus temp inlined
    assert (value["from"] if isinstance(value, dict) else getattr(value, "from")) == value["from"]
    assert isinstance(value["from"], str) and isinstance(value["class"], int)


def _op_spec(**overrides):
    spec = {
        "params": [{"name": "k", "type": "Str"}, {"name": "v", "type": "Str"}],
        "returns": "Int",
        "emission": False,
        "async": False,
    }
    spec.update(overrides)
    return spec


def test_make_operation_returns_deterministic_typed_sequence():
    """Per-operation seeding: two identically-named ops give the identical
    response sequence, and every response is a value of the declared type."""
    recorder = mocks_mod.MockRecorder()
    args = ("k", "v")
    op_a = mocks_mod._make_operation("Database", "db", "execute", _op_spec(),
                                     {}, _types.SimpleNamespace(), recorder)
    op_b = mocks_mod._make_operation("Database", "db", "execute", _op_spec(),
                                     {}, _types.SimpleNamespace(), recorder)
    seq_a = [op_a(*args) for _ in range(5)]
    seq_b = [op_b(*args) for _ in range(5)]
    assert seq_a == seq_b
    assert all(isinstance(v, int) for v in seq_a)


def test_make_operation_records_emissions_not_crossed():
    """An emission-classified op records the crossing (with the arguments that
    would have crossed) and still returns a generated value — it never makes
    the crossing itself."""
    recorder = mocks_mod.MockRecorder()
    op = mocks_mod._make_operation("Database", "db", "execute",
                                   _op_spec(emission=True),
                                   {}, _types.SimpleNamespace(), recorder)
    value = op("select 1")
    assert isinstance(value, int)
    assert recorder.count() == 1
    assert recorder.crossings[0]["service"] == "Database"
    assert recorder.crossings[0]["key"] == "db"
    assert "sql='select 1'" in recorder.crossings[0]["args"] or \
        "k='select 1'" in recorder.crossings[0]["args"]
    report = "\n".join(recorder.report())
    assert "1 crossing recorded, none made" in report
    assert "would have emitted:" in report


def test_make_operation_async_returns_awaitable():
    import asyncio

    recorder = mocks_mod.MockRecorder()
    op = mocks_mod._make_operation("Fetch", "fetch", "fetch",
                                   {"params": [{"name": "url", "type": "Str"}],
                                    "returns": "Str", "emission": False, "async": True},
                                   {}, _types.SimpleNamespace(), recorder)
    result = op("https://example.com")
    assert hasattr(result, "__await__")
    assert isinstance(asyncio.run(result), str)


def test_recorder_report_groups_and_is_empty_when_nothing_crossed():
    empty = mocks_mod.MockRecorder()
    assert empty.report() == []
    recorder = mocks_mod.MockRecorder()
    recorder.record("Database", "db", "execute", ["sql"], ("select 1",), "Int")
    recorder.record("Database", "db", "execute", ["sql"], ("select 2",), "Int")
    recorder.record("Database", "db", "ping", [], (), "Bool")
    lines = recorder.report()
    assert len(lines) == 6  # header + 2 group lines + 3 would-have lines
    text = "\n".join(lines)
    assert "db.execute (Database) — 2 crossings recorded, none made" in text
    assert "db.ping (Database) — 1 crossing recorded, none made" in text
    assert "would have emitted: sql='select 1'" in text
    assert "would have emitted: (no arguments)" in text


def test_make_mock_component_shape():
    """The mock is a runtime-constructed cordis component of the exact shape
    the emitter renders for a real provider."""

    class FakeFrame:
        def __init__(self, ctx, name):
            self.installed = None

        def install(self, body):
            self.installed = body

    comp = mocks_mod.make_mock_component(
        "db", "Database", {"methods": {"execute": _op_spec()}}, {},
        _types.SimpleNamespace(), mocks_mod.MockRecorder(), FakeFrame)
    assert comp["name"] == "_RevlMock_Database_db"
    assert comp["inject"] == []
    assert callable(comp["apply"])


# ----------------------------------------------------------------------------
# wiring — the --mock-requires CLI paths (no runtime needed)
# ----------------------------------------------------------------------------


def test_mock_requires_with_no_lifecycle_tests_is_a_noop(capsys, monkeypatch):
    ir = compile_source('''
service KV { fn count() -> Int }
component RealKV provides kv: KV {
  provide kv { fn count() { return 1 } }
}
test "pure" { assert true == true }
''', "pure.rvl")
    monkeypatch.setattr("revl.test._cordis_available", lambda: False)
    code = _test_command(ir, "py", mock_requires=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "no `lifecycle test` to mock" in out


def test_mock_requires_skips_with_a_reason_when_runtime_absent(capsys, monkeypatch):
    ir = compile_source(DB_APP, "db_app.rvl")
    monkeypatch.setattr("revl.test._cordis_available", lambda: False)
    code = _test_command(ir, "py", mock_requires=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "[mock-requires] skipped" in out
    assert "cordis-py runtime" in out


def test_mock_requires_backend_note_for_non_py(capsys, monkeypatch):
    ir = compile_source(DB_APP, "db_app.rvl")
    monkeypatch.setattr("revl.test._cordis_available", lambda: False)
    code = _test_command(ir, "go", mock_requires=True)
    out = capsys.readouterr().out
    assert code == 0
    assert "mock world runs on the py reference tier only, not `go`" in out
    assert "[mock-requires] skipped" in out


def test_strip_to_base_keeps_tests_drops_fault_and_prop_sections():
    """The base module keeps `tests` (the driver evaluates test-body
    expressions through the backend renderer, which references module-scope
    helpers gated on the whole IR) but drops fault/prop sections — a fault
    sweep splices `fail` steps into the very components mock world boots."""
    ir = compile_source(DB_APP + '''
fault test "db dies" for App {
  fail at step 1
  assert failed
  assert no residue
  assert inverses lifo
  assert siblings unaffected
}
''', "mixed.rvl")
    base = mocks_mod._strip_to_base(ir)
    assert base.get("tests") == ir.get("tests")
    assert "fault_tests" not in base
    assert "prop_tests" not in base


def test_strip_to_base_emits_expression_helpers():
    """Regression: stripping the tests dropped the triggers that gate
    `_revl_i64` & friends, so a test-body Int expression NameError'd in mock
    world. The base module must still carry every helper the driver can
    render."""
    ir = compile_source(SHAPES, "shapes.rvl")
    source = _emitter("python").emit(mocks_mod._strip_to_base(ir))
    assert "def _revl_i64" in source


# ----------------------------------------------------------------------------
# execution — real compositions in mock world (needs cordis-py)
# ----------------------------------------------------------------------------


@needs_cordis
def test_a_composition_with_mock_requires_boots_with_zero_setup(capsys):
    """(a) A consumer whose every `requires` is met by a generated mock boots
    and lifecycle-tests with no real providers and no setup code."""
    code, out = _run_mock_world(DB_APP, capsys)
    assert code == 0, out
    assert "PASS app runs in mock world [mock world]" in out
    assert "1 passed, 0 failed" in out


@needs_cordis
def test_mocked_emission_is_recorded_not_crossed(capsys):
    """(c) The report says what would have been emitted — the count and the
    arguments — and nothing crossed."""
    code, out = _run_mock_world(DB_APP, capsys)
    assert code == 0, out
    assert "1 emission recorded-not-crossed" in out
    assert "db.execute (Database) — 1 crossing recorded, none made" in out
    assert "would have emitted: sql='select 1'" in out
    assert "never made one" in out


@needs_cordis
def test_mocked_operations_return_typed_deterministic_values(capsys):
    """(b) Mock responses are typed: each bound value must survive the
    type-shape assertions in SHAPES (an Int that is not an int would TypeError
    at `n - n`, a non-str at `s + "x"`, a non-bool at `(b == true) == b`)."""
    code, out = _run_mock_world(SHAPES, capsys)
    assert code == 0, out
    assert "PASS mock values keep their declared shapes [mock world]" in out


@needs_cordis
def test_mock_world_is_reproducible_across_runs(capsys):
    """(d) Two full mock-world runs of the same document produce identical
    output — the per-operation seeds make the whole run reproducible."""
    code, out = _run_mock_world(SHAPES, capsys)
    code2, out2 = _run_mock_world(SHAPES, capsys)
    assert code == code2 == 0
    assert out == out2


@needs_cordis
def test_a_real_provider_loaded_first_keeps_its_place(capsys):
    """Mocks fill only unmet requires: a real provider loaded before the
    consumer is what the consumer binds to."""
    code, out = _run_mock_world(PRECEDENCE, capsys)
    assert code == 0, out
    assert "PASS real provider wins over the mock [mock world]" in out
    assert "PASS mock fills requires with zero setup [mock world]" in out


@needs_cordis
def test_two_consumers_share_one_mock_and_reload_gets_a_fresh_one(capsys):
    code, out = _run_mock_world(SHARED, capsys)
    assert code == 0, out
    assert "PASS two consumers share one mock [mock world]" in out
    assert "PASS a consumer can reload against a fresh mock [mock world]" in out


@needs_cordis
def test_record_and_adt_responses_are_constructed(capsys):
    code, out = _run_mock_world(RECORDS, capsys)
    assert code == 0, out
    assert "PASS record and ADT responses are constructed [mock world]" in out


@needs_cordis
def test_a_failing_assertion_fails_the_run(capsys):
    code, out = _run_mock_world(FAILING, capsys)
    assert code == 1
    assert "FAIL a failing assertion fails [mock world]" in out
    assert "0 passed, 1 failed" in out


@needs_cordis
def test_leaving_a_mock_loaded_is_caught_as_residue(capsys):
    """The residue check is not a rubber stamp: a consumer left loaded keeps
    its mock's provision live, and `assert no_residue` names it."""
    code, out = _run_mock_world(RESIDUE, capsys)
    assert code == 1
    assert "FAIL leaving a mock loaded is residue [mock world]" in out
    assert "provisions: [] -> ['api', 'kv']" in out
    assert "(R4)" in out


@needs_cordis
def test_pure_tests_are_not_part_of_mock_world(capsys):
    """--mock-requires runs lifecycle tests only; the document's pure test
    block is left to the ordinary runner."""
    code, out = _run_mock_world(MIXED, capsys)
    assert code == 0, out
    assert "PASS the lifecycle test runs in mock world [mock world]" in out
    assert "a pure test is not part of mock world" not in out


def _run_mock_world(block: str, capsys) -> tuple:
    ir = compile_source(block, "mock.rvl")
    code = _test_command(ir, "py", mock_requires=True)
    out = capsys.readouterr().out
    return code, out


# ----------------------------------------------------------------------------
# CLI — the whole subprocess under the backend's own venv
# ----------------------------------------------------------------------------


def _revl_mock(*args: str) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([str(CORDIS_PY), "-m", "revl", "test", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)


@needs_venv
def test_cli_mock_requires_end_to_end(tmp_path):
    path = tmp_path / "db_app.rvl"
    path.write_text(DB_APP, encoding="utf-8")
    result = _revl_mock(str(path), "--mock-requires")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS app runs in mock world [mock world]" in result.stdout
    assert "db.execute (Database) — 1 crossing recorded, none made" in result.stdout
    assert "would have emitted: sql='select 1'" in result.stdout


@needs_venv
def test_cli_mock_requires_is_reproducible_across_processes(tmp_path):
    path = tmp_path / "db_app.rvl"
    path.write_text(DB_APP, encoding="utf-8")
    first = _revl_mock(str(path), "--mock-requires")
    second = _revl_mock(str(path), "--mock-requires")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


@needs_venv
def test_cli_failing_mock_world_exits_nonzero(tmp_path):
    path = tmp_path / "failing.rvl"
    path.write_text(FAILING, encoding="utf-8")
    result = _revl_mock(str(path), "--mock-requires")
    assert result.returncode == 1
    assert "FAIL a failing assertion fails [mock world]" in result.stdout
