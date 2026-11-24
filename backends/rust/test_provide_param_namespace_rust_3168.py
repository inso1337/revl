"""Issue #3168, rust tier: a provide-method parameter in the helper namespace.

A user value spelled `revl_*`/`REVL_*`/`_revl*`/`_REVL*` takes one `_` so it
cannot shadow an emitted helper. A provide-method body reads its parameters
through that escape, so the method head, the undo clones, the effect argument
clones and the secret marks must spell the parameter the same way. The head
used to keep the source spelling, so `fn add(&self, revl_n: i64)` read
`revl_n_` and rustc refused the module (E0425).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import compile_source  # noqa: E402

import test_emit_rust as rust_tests  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "rust_emit_3168_provide", ROOT / "backends" / "rust" / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

PROGRAM = """\
service Meter {
  fn add(revl_n: Int) -> Int
  fn keep(revl_k: Str, revl_v: Str) -> Str
}

component Gauge provides meter: Meter {
  let store = effect Map.new() undo store.drop()

  provide meter {
    fn add(revl_n) = revl_n + 1
    fn keep(revl_k, revl_v) {
      let revl_key = revl_k
      effect store.insert(revl_k, revl_v) undo store.remove(revl_k)
      return revl_v
    }
  }
}
"""

LIFECYCLE = """
lifecycle test "a provide parameter in the helper namespace" {
  load Gauge
  let a = call meter.add(41)
  assert a == 42
  let b = call meter.keep("k", "v")
  assert b == "v"
}
"""


def _emit(source: str) -> str:
    return emit.emit(compile_source(source, "provide_params.rvl"))


def test_the_method_head_spells_the_parameter_its_body_reads():
    src = _emit(PROGRAM)
    assert "fn add(&self, revl_n_: i64) -> i64 { (revl_n_).checked_add(" in src
    assert "fn keep(&self, revl_k_: String, revl_v_: String) -> String {" in src
    assert "let revl_k_undo = revl_k_.clone();" in src
    assert "self.store.insert(revl_k_.clone(), revl_v_.clone())" in src
    assert "fn add(&self, revl_n: i64) -> i64 { (revl_n_)" not in src


@rust_tests.needs_cargo
def test_a_provide_parameter_in_the_helper_namespace_builds_and_runs(tmp_path):
    crate = tmp_path / "crate"
    crate.mkdir()
    result = rust_tests._cargo_test(crate, _emit(PROGRAM + LIFECYCLE), "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a_provide_parameter_in_the_helper_namespace ... ok" in result.stdout
