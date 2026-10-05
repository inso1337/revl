"""Issue #1931: an activation `let`-effect bind is held by value, typed by what
its acquisition evaluates to, for every acquisition that is not a shared
resource.

#1920 held an `extern acquire` handle by value. A required-service call
(`let r = effect store.grab(2) undo ..`) still bound `Arc::new(..)` against an
`Arc<Value>` provider field, and the crate did not build (E0308). The rule is
now general: a host object, a spawn handle, a subscription and the
result-declared host CAS stay `Arc`-held; any other acquisition is held as the
type it evaluates to (the inference a provide-method `let` takes), and a
provide method reads it through `&self` as a clone.

The shape is tests/fixtures/emit_rust_corpus/held_acquire_binds.rvl, which the
self-host port also holds to byte agreement.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import compile_files  # noqa: E402

import test_emit_rust as rust_tests  # noqa: E402

_spec = importlib.util.spec_from_file_location("rust_emit_1931", ROOT / "backends" / "rust" / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

DOC = ROOT / "tests" / "fixtures" / "emit_rust_corpus" / "held_acquire_binds.rvl"

LIFECYCLE = '''
lifecycle test "value-held activation binds" {
  load Shop
  load Reader
  let t = call front.total()
  assert t == 45
  let l = call front.label()
  assert l == "n"
}
'''


def _emit(path: Path) -> str:
    return emit.emit(compile_files([str(path)]))


def test_a_value_acquisition_is_held_by_its_type():
    src = _emit(DOC)
    # a required-service call's return and a module fn's record, by value
    assert "    r: i64," in src and "let r = store.grab(2i64);" in src
    assert "    n: Note," in src and "let n = note_for(3i64);" in src
    assert "self.r.clone()" in src and "self.n.clone().size" in src
    # the host Map is a shared resource and stays Arc-held
    assert "    seen: Arc<Map<String>>," in src
    assert "let seen = Arc::new(Map::<String>::new());" in src
    assert "Arc<Value>" not in src


@rust_tests.needs_cargo
def test_value_held_binds_build_and_run(tmp_path):
    path = tmp_path / "held_acquire_binds.rvl"
    path.write_text(DOC.read_text(encoding="utf-8") + LIFECYCLE, encoding="utf-8")
    crate = tmp_path / "crate"
    crate.mkdir()
    result = rust_tests._cargo_test(crate, _emit(path), "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "value_held_activation_binds ... ok" in result.stdout
