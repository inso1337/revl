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
pub fn keep(h: Holder) -> Holder { return h }
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


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not installed")
def test_value_carrying_declarations_build(tmp_path):
    """End-to-end: the emitted crate must actually `cargo build`."""
    m = _load_rust_emit()
    src = m.emit(compile_source(NESTED_VALUE, "nested_value.rvl"))
    crate = tmp_path / "crate"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text(m.cargo_toml("revl_value3019"),
                                      encoding="utf-8")
    (crate / "src" / "lib.rs").write_text(src, encoding="utf-8")
    result = subprocess.run(["cargo", "build", "--quiet"], cwd=crate,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
