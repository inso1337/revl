"""A record or variant field of type `Value` must build on the Rust tier (#3019).

`Value` (stdlib/value.rvl, docs/stdlib-value.md) is the NAMED erased-dynamic
builtin and erases to the opaque ``cordis::Value`` on this tier. ``cordis::Value``
implements ``Clone`` and ``Debug`` and nothing else: no ``PartialEq``, no serde
traits. ``_emit_v3_types`` derived
``Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize`` on every record
and variant, so a legal revl declaration whose field mentions ``Value`` produced
a crate rustc rejects with E0369 (``Vec<Value>`` / ``Value`` have no ``==``) and
E0277 (``Value: serde::Serialize`` is not satisfied) (issue #3019).

The erased value supports only ``Clone`` and ``Debug``, so a declaration that
CONTAINS one (directly, or through a nested declaration, or in a variant
payload) now derives exactly those two. Structural equality and wire marshalling
of an erased field are not available on this tier, exactly as they are not for
an opaque ``Value`` itself; a declaration that does not contain one keeps the
full derive set.

A concrete value entering an erased field takes the same coercion a ``Value``
parameter takes, so ``{ v: n }`` against ``v: Value`` boxes the ``i64`` instead
of failing with E0308.

The end-to-end test builds the emitted crate with cargo, so the claim is that
the generated Rust compiles, not that the source looks right.
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
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402

VALUE_RECORD = """\
pub type Box = { tags: List[Value], v: Value, m: Map[Str, Value] }
"""

# Direct fields, a nested record that reaches `Value` through another one, a
# record reached through a variant payload, and a Value-free control record.
NESTED_VALUE = """\
type Inner = { v: Value }
type Outer = { inner: Inner, items: List[Inner] }
type Maybe = Has(Value) | Missing
type Holder = { tag: Str, inner: Inner, outer: Outer, maybe: Maybe }
pub type Plain = { n: Int }
pub type Box = { tags: List[Value], v: Value, m: Map[Str, Value] }
pub fn keep(h: Holder) -> Holder { return h }
pub fn make(n: Int) -> Box { return { tags: [], v: n, m: Map.empty() } }
"""

# A Value-carrying record crossing the generated interop bridge. The record has
# no serde impls, so the bridge must treat it as opaque (like an erased `Value`)
# instead of emitting `serde_json::to_value`/`from_value` for it, including
# inside a `Result`. A `HashMap` declaration that carries a `Value` must not
# make an unrelated `Map[Str, Int]` parameter look opaque: its rendered
# `std::collections::HashMap<..>` path must not be read as the declaration.
BRIDGE_VALUE = """\
service Store {
  fn get() -> Blob
  fn put(b: Blob) -> Str
  fn getr() -> Result[Blob, Str]
  fn putr(r: Result[Blob, Str]) -> Str
  fn count(m: Map[Str, Int]) -> Int
}
type Blob = { v: Value, n: Int }
type HashMap = { v: Value }
component C provides store: Store {
  provide store {
    fn get() = { v: 1, n: 2 }
    fn put(b) = "ok"
    fn getr() = Ok({ v: 1, n: 2 })
    fn putr(r) = "ok"
    fn count(m) = 1
  }
}
"""

FULL_DERIVE = ("#[derive(Clone, Debug, PartialEq, "
               "serde::Serialize, serde::Deserialize)]")
ERASED_DERIVE = "#[derive(Clone, Debug)]"


def _load_rust_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_rust_emit_value3019", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _derive_of(out: str, item: str) -> str:
    """The `#[derive(..)]` line immediately preceding one emitted item."""
    head = out[:out.index(item)]
    return [line for line in head.splitlines()
            if line.startswith("#[derive(")][-1]


def test_record_with_value_derives_only_what_cordis_value_supports():
    m = _load_rust_emit()
    out = m.emit(compile_source(VALUE_RECORD, "value_record.rvl"))
    assert _derive_of(out, "pub struct Box_") == ERASED_DERIVE
    assert "serde::Serialize" not in out


def test_nested_and_variant_value_fields_reduce_too():
    """The reduction follows containment, not just a literal `Value` field:
    `Outer` reaches it through `Inner`, `Maybe` carries it as a case payload,
    and `Holder` reaches it through both. A Value-free record keeps the full
    derive set."""
    m = _load_rust_emit()
    out = m.emit(compile_source(NESTED_VALUE, "nested_value.rvl"))
    assert _derive_of(out, "pub struct Inner") == ERASED_DERIVE
    assert _derive_of(out, "pub struct Outer") == ERASED_DERIVE
    assert _derive_of(out, "pub enum Maybe") == ERASED_DERIVE
    assert _derive_of(out, "pub struct Holder") == ERASED_DERIVE
    assert _derive_of(out, "pub struct Plain") == FULL_DERIVE


def test_concrete_value_flowing_into_a_value_field_is_boxed():
    """A value entering an erased `Value` field takes the same coercion a
    `Value` parameter gets: the field holds `cordis::Value` and a concrete
    `i64` does not implement `Into<Value>`, so `{ v: n }` must box it or the
    struct literal is E0308. `Box` also covers the mangled-name lookup: the
    record emits as `Box_`, and its declared field types must still be found."""
    m = _load_rust_emit()
    out = m.emit(compile_source(NESTED_VALUE, "nested_value.rvl"))
    assert "v: Value::new(serde_json::Value::from(n))" in out


def test_record_field_types_resolve_raw_and_emitted_names():
    """The field map is reached with the RAW declaration name from
    `var_types`/`_v3_infer_type` (a mangled record reads as `Box`) and with the
    EMITTED ident from a record literal (`Box_`). Resolving only the emitted
    side silently dropped field typing on the read path."""
    m = _load_rust_emit()
    ctx = m._V3Ctx({"Box": {"kind": "record", "fields": {"v": "Value"}}},
                   [], [])
    assert ctx.record_field_types("Box") == {"v": "Value"}
    assert ctx.record_field_types("Box_") == {"v": "Value"}
    assert ctx.record_field_types("Nope") == {}


def test_a_declared_mangled_name_is_not_shadowed_by_its_source_name():
    """`Box` emits as `Box_` and `Box_` as `Box__`, so a lookup of the RAW
    `Box_` must return the `Box_` declaration and not the `Box` that also
    matches the mangled spelling; the reverse map is built once, so a lookup is
    a dict probe rather than an `_ident` scan over every declared type."""
    m = _load_rust_emit()
    ctx = m._V3Ctx({
        "Box": {"kind": "record", "fields": {"v": "Value"}},
        "Box_": {"kind": "record", "fields": {"n": "Int"}},
    }, [], [])
    assert ctx.record_field_types("Box") == {"v": "Value"}
    assert ctx.record_field_types("Box_") == {"n": "Int"}
    assert ctx.record_field_types("Box__") == {"n": "Int"}  # Box_'s emitted ident
    assert ctx.declaration_params("Box_") == frozenset()


def test_any_field_reduces_like_value():
    """Every surface that erases to the opaque `Value` counts: the wildcard
    `Any` renders `Value` too, so a record field of it cannot derive the full
    set either."""
    m = _load_rust_emit()
    out = m.emit(compile_source("type WithAny = { a: Any }\n", "any_field.rvl"))
    assert _derive_of(out, "pub struct WithAny") == ERASED_DERIVE


def test_a_user_type_name_colliding_with_a_prelude_path_is_not_containment():
    """A user record named `HashMap` emits `HashMap`, and an unrelated
    `Map[Str, Int]` field renders `std::collections::HashMap<..>`. Counting the
    rendered spelling as containment silently dropped `PartialEq`/serde from a
    record that holds no erased value, so the walk resolves declarations by
    surface name instead."""
    m = _load_rust_emit()
    out = m.emit(compile_source(
        "type HashMap = { v: Value }\n"
        "type Uses = { m: Map[Str, Int] }\n", "name_collision.rvl"))
    assert _derive_of(out, "pub struct HashMap") == ERASED_DERIVE
    assert _derive_of(out, "pub struct Uses") == FULL_DERIVE


def test_a_variant_type_parameter_shadows_a_declaration():
    """`T` in `type Jj[T] = A(T)` names the parameter, not the declared record
    `T`, so `Jj` contains no erased value and keeps the full derive set."""
    m = _load_rust_emit()
    out = m.emit(compile_source(
        "type T = { v: Value }\n"
        "type Jj[T] = A(T)\n", "shadowed_param.rvl"))
    assert _derive_of(out, "pub struct T ") == ERASED_DERIVE
    assert _derive_of(out, "pub enum Jj") == FULL_DERIVE


def test_bridge_treats_a_value_carrying_record_as_opaque():
    """The record derives no serde traits, so the generated bridge must not
    marshal it: a parameter type with no wire form is refused at dispatch and a
    return is left to the proxy's unsupported-return path, as for a bare
    `Value`. That includes either half of a `Result`, whose canonical encoding
    serde-serializes both halves. A carrying declaration merely NAMED `HashMap`
    must not make an unrelated `Map[Str, Int]` parameter opaque through the
    rendered `std::collections::HashMap<..>` path."""
    m = _load_rust_emit()
    out = m.emit(compile_source(BRIDGE_VALUE, "bridge_value.rvl"))
    assert "from_value::<Blob" not in out
    assert "from_value::<Result" not in out
    assert '"$kind"' not in out  # no canonical Result encoding was built
    assert "serde_json::to_value(&b)" not in out
    assert "serde_json::to_value(&r)" not in out  # Result param is opaque
    assert "serde_json::to_value(&m)" in out  # Map param still marshals
    assert 'from_value::<std::collections::HashMap<String, i64>>' in out
    assert "cannot be called across a seam" in out


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
@pytest.mark.parametrize("label,source", [
    ("nested", NESTED_VALUE),
    ("bridge", BRIDGE_VALUE),
])
def test_value_carrying_declarations_build(label, source, tmp_path):
    """End-to-end: the emitted crate must actually `cargo build`."""
    m = _load_rust_emit()
    src = m.emit(compile_source(source, f"{label}_value.rvl"))
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(m.cargo_toml(f"revl_value3019_{label}"),
                                      encoding="utf-8")
    (crate / "src" / "lib.rs").write_text(src, encoding="utf-8")
    result = subprocess.run(["cargo", "build", "--quiet"], cwd=crate,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
