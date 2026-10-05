"""Issue #1927: a requirement key that is a Rust keyword (`box`, `ref`, `mut`,
...) was emitted raw (`let box = ctx.require::<..>("box")?;`, a `box:` field)
and the crate did not build.

The key's local, its provider-struct field, its `self.` capture and every
reclone now take the mangled spelling (`box_`), the one the expression
renderer already used for a `req` node. The inject gate and the
`ctx.require::<..>("box")` string keep the surface key. Activation binds
and method-body locals reached the same raw `.clone()` sites and take the
mangled spelling there too.

The shape is tests/fixtures/emit_rust_corpus/keyword_keys.rvl, which the
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

_spec = importlib.util.spec_from_file_location("rust_emit_1927", ROOT / "backends" / "rust" / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

DOC = ROOT / "tests" / "fixtures" / "emit_rust_corpus" / "keyword_keys.rvl"

LIFECYCLE = '''
lifecycle test "a requirement keyed by a rust keyword" {
  load Shop
  load Reader
  let v = call front.read(1)
  assert v == 8
}
'''


def _emit(path: Path) -> str:
    return emit.emit(compile_files([str(path)]))


def test_a_keyword_requirement_key_is_mangled_where_it_is_an_identifier():
    src = _emit(DOC)
    assert 'let box_ = ctx.require::<Box<dyn Store>>("box")?;' in src
    assert "    box_: Arc<Box<dyn Store>>," in src
    assert "box_: box_.clone()" in src
    assert "let box_undo = self.box_.clone();" in src
    assert "let box_t1 = box_.clone();" in src
    assert "let mut_ = self.box_.get();" in src
    # the surface key stays in the inject gate
    assert 'cordis::Inject::new(["box"])' in src
    for raw in ("let box ", "    box:", "self.box.", "{ box:"):
        assert raw not in src, raw


@rust_tests.needs_cargo
def test_a_keyword_requirement_key_builds_and_runs(tmp_path):
    path = tmp_path / "keyword_keys.rvl"
    path.write_text(DOC.read_text(encoding="utf-8") + LIFECYCLE, encoding="utf-8")
    crate = tmp_path / "crate"
    crate.mkdir()
    result = rust_tests._cargo_test(crate, _emit(path), "")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a_requirement_keyed_by_a_rust_keyword ... ok" in result.stdout
