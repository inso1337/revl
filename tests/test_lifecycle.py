"""`lifecycle test` — frontend, IR shape, and per-tier portability (§7.1).

Execution against the real cordis-py runtime lives in test_lifecycle_exec.py;
this file covers everything that needs no runtime.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

EXAMPLES = ROOT / "examples"

PAIR = """
service Store {
  fn get(key: Str) -> Str
  emission fn put(key: Str, value: Str) -> Int
}

component Kv provides kv: Store {
  config { size: Int = 16, label: Str }
  let m = effect Map.new() undo m.drop()
  provide kv {
    fn get(key) = m.get(key)
    fn put(key, value) {
      effect m.insert(key, value)
      undo   m.remove(key)
      return 1
    }
  }
}
"""


def _emitter(backend: str):
    spec = importlib.util.spec_from_file_location(
        f"revl_{backend}_emit_lifecycle", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ IR shape

def test_lifecycle_test_lowers_to_a_tagged_test_unit():
    ir = compile_source(PAIR + """
    lifecycle test "round trip" {
      load Kv with { label: "x" }
      call kv.put("k", "v")
      let hit = call kv.get("k")
      assert hit == "v"
      unload Kv
      assert no_residue
    }
    """)
    assert ir["ir_version"] == 3
    (unit,) = ir["tests"]
    assert unit["name"] == "round trip"
    assert unit["lifecycle"] is True
    assert [step["step"] for step in unit["body"]] == [
        "load", "call", "call", "assert", "unload", "assert_no_residue"]
    load, put, get = unit["body"][0], unit["body"][1], unit["body"][2]
    assert load["component"] == "Kv"
    assert load["config"] == {"label": {"kind": "lit", "value": "x"}}
    assert (put["key"], put["method"]) == ("kv", "put")
    assert "bind" not in put
    assert get["bind"] == "hit"


def test_a_plain_test_unit_is_unchanged_by_this_feature():
    """The `tests` entry for a pure test carries no lifecycle key at all, so
    every existing document lowers byte-identically."""
    ir = compile_source('test "pure" { assert 1 + 1 == 2 }')
    (unit,) = ir["tests"]
    assert set(unit) == {"name", "body"}


def test_config_defaults_may_be_omitted_but_required_fields_may_not():
    compile_source(PAIR + 'lifecycle test "t" { load Kv with { label: "x" } unload Kv }')
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'lifecycle test "t" { load Kv unload Kv }')
    assert "missing required config `label`" in str(excinfo.value)


def test_config_value_is_type_checked_against_the_field():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'lifecycle test "t" { load Kv with { label: 1 } unload Kv }')
    assert "config field `label` of Kv" in str(excinfo.value)
    assert "expects `Str`" in str(excinfo.value)


def test_call_arguments_are_checked_against_the_service_declaration():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + """
        lifecycle test "t" { load Kv with { label: "x" } call kv.get(1) unload Kv }
        """)
    assert "`kv.get` argument `key` expects `Str`, got `Int`" in str(excinfo.value)

    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + """
        lifecycle test "t" { load Kv with { label: "x" } call kv.get("a", "b") unload Kv }
        """)
    assert "`kv.get` takes 1 argument(s), 2 given" in str(excinfo.value)


def test_a_binding_takes_the_operations_declared_return_type():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + """
        lifecycle test "t" {
          load Kv with { label: "x" }
          let hit = call kv.get("k")
          assert hit == 1
          unload Kv
        }
        """)
    assert "Str" in str(excinfo.value) and "Int" in str(excinfo.value)


def test_calling_through_a_key_whose_provider_is_not_loaded_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'lifecycle test "t" { call kv.get("k") }')
    assert "no provider for key `kv` at this point" in str(excinfo.value)

    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + """
        lifecycle test "t" {
          load Kv with { label: "x" }
          unload Kv
          call kv.get("k")
        }
        """)
    assert "no provider for key `kv` at this point" in str(excinfo.value)


def test_unloading_something_that_is_not_loaded_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'lifecycle test "t" { unload Kv }')
    assert "`Kv` is not loaded at this point" in str(excinfo.value)


def test_calling_an_emission_operation_needs_no_marker():
    """G4 bounds *providers*; a lifecycle test is not one (§7.1)."""
    ir = compile_source(PAIR + """
    lifecycle test "t" {
      load Kv with { label: "x" }
      call kv.put("k", "v")
      unload Kv
    }
    """)
    assert ir["tests"][0]["body"][1]["method"] == "put"


def test_lifecycle_is_a_contextual_keyword_not_a_lexer_keyword():
    """Nothing may stop compiling because these words became reserved."""
    from revl.lexer import KEYWORDS

    for word in ("lifecycle", "load", "unload", "call", "no_residue"):
        assert word not in KEYWORDS
    ir = compile_source("""
    fn f() -> Int {
      let load = 1
      let unload = 2
      let call = 3
      let lifecycle = 4
      let no_residue = 5
      return load + unload + call + lifecycle + no_residue
    }
    """)
    assert ir["functions"][0]["name"] == "f"


def test_lifecycle_without_test_is_named():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + "lifecycle fn oops() -> Int { return 1 }")
    assert "`lifecycle` is a modifier on `test`" in str(excinfo.value)


def test_var_in_a_lifecycle_body_is_refused():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'lifecycle test "t" { var x = call kv.get("k") }')
    assert "`var` has no meaning in a lifecycle test" in str(excinfo.value)


def test_a_non_lifecycle_statement_in_a_lifecycle_body_is_named():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'lifecycle test "t" { return 1 }')
    assert "expected a lifecycle statement" in str(excinfo.value)


def test_duplicate_test_names_across_both_forms():
    with pytest.raises(RevlError) as excinfo:
        compile_source(PAIR + 'test "same" { assert true }\nlifecycle test "same" { }')
    assert "duplicate test `same`" in str(excinfo.value)


# ------------------------------------------------------------------ examples

def test_example_compiles():
    ir = compile_files([str(EXAMPLES / "lifecycle_cache.rvl")])
    names = [test["name"] for test in ir["tests"]]
    assert names == ["cache reverts cleanly", "a reloaded cache starts empty"]
    assert all(test["lifecycle"] for test in ir["tests"])


def test_leaky_example_compiles_cleanly():
    """The point of the negative case: the leak is invisible to every static
    check — the document is accepted, and only the lifecycle test catches it."""
    ir = compile_files([str(EXAMPLES / "lifecycle_leak.rvl")])
    assert ir["tests"][0]["lifecycle"] is True


# --------------------------------------------------------------- portability

def test_reference_tier_emits_a_driver():
    ir = compile_files([str(EXAMPLES / "lifecycle_cache.rvl")])
    source = _emitter("python").emit(ir)
    assert "_revl_no_residue" in source
    assert "from cordis import Context" in source
    compile(source, "<emitted>", "exec")  # it is at least valid Python


@pytest.mark.parametrize("backend,tier", [
    ("wasm", "wasm"),
])
def test_other_tiers_refuse_by_name(backend, tier):
    """The wasm substrate refuses, by name, what it cannot express — a
    construct silently dropped by one renderer and honored by another is this
    project's recurring bug class. Every hosted tier lowers lifecycle tests
    (see test_lowerable_tiers below)."""
    ir = compile_files([str(EXAMPLES / "lifecycle_cache.rvl")])
    emitter = _emitter(backend)
    with pytest.raises(emitter.EmitError) as excinfo:
        emitter.emit(ir)
    message = str(excinfo.value)
    assert "lifecycle test 'cache reverts cleanly'" in message
    assert f"not lowerable on the {tier} tier" in message
    assert "--backend py" in message


def test_java_refuses_lifecycle_tests_below_ir_version_3():
    """item 178(b): java drives lifecycle tests on ir_version 3; the older
    dialects have no test machinery there at all, so a lifecycle test in one is
    refused BY NAME, in the wording `revl test` reads back as a
    skip-with-reason rather than a tier failure."""
    ir = compile_files([str(EXAMPLES / "lifecycle_cache.rvl")])
    ir["ir_version"] = 1
    emitter = _emitter("java")
    with pytest.raises(emitter.EmitError) as excinfo:
        emitter.emit(ir)
    message = str(excinfo.value)
    assert "lifecycle test 'cache reverts cleanly'" in message
    assert "not lowerable on the cordis4j tier" in message
    assert "--backend py" in message


@pytest.mark.parametrize("backend,marker", [
    ("rust", "revl_lifecycle_"),
    ("typescript", "assertNoResidue"),
    ("go", "revlOptPair"),
    ("java", "revlLifecycleNoResidue"),
])
def test_lowerable_tiers_emit_lifecycle_drivers(backend, marker):
    """FR-5 + item 178(b): rust, ts, go and java lower lifecycle tests to their
    native test idiom — a real driver over a live composition, ending in the
    tier's no-residue assertion — instead of refusing them."""
    ir = compile_files([str(EXAMPLES / "lifecycle_cache.rvl")])
    source = _emitter(backend).emit(ir)
    assert "cache reverts cleanly" in source
    assert "no_residue" in source or "residue" in source
    assert marker in source


@pytest.mark.parametrize("backend", ["python", "rust", "java", "typescript", "wasm"])
def test_pure_tests_still_emit_on_every_tier(backend):
    """The refusal is scoped to lifecycle units — plain `test` is untouched."""
    ir = compile_source('test "pure" { assert 1 + 1 == 2 }')
    assert _emitter(backend).emit(ir)
