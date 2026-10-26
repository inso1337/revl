"""A by-value argument read once in a `return` moves instead of cloning
(issue #2905).

The Rust backend cloned every non-Copy by-value argument, even on its last
use: `return p_stmts(ts.clone(), .., acc.clone())` where neither is read
again. A name a `return` expression reads exactly once, outside any arrow, is
dead once the function returns, so it now moves. A second read in the same
return still clones, and so does a name captured inside an arrow. The
self-host port follows byte for byte (tests/test_selfhost_emit_rust.py,
`return_moves.rvl`), and that file's cargo build-and-run test builds the
fixture on both emitters, so rustc checks every move (E0382).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_rust_emit_2905", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

DOC = ROOT / "tests" / "fixtures" / "emit_rust_corpus" / "return_moves.rvl"


@pytest.fixture(scope="module")
def rust() -> str:
    return emit.emit(compile_files([str(DOC)]))


def _body(rust: str, name: str) -> str:
    return rust.split(f"fn {name}(")[1].split("\n}")[0]


def test_a_name_read_once_in_a_return_moves(rust):
    assert "return bump(t, 1i64);" in _body(rust, "bump_once")


def test_a_name_read_twice_in_one_return_still_clones(rust):
    assert "return merge(t.clone(), bump(t.clone(), 1i64));" in _body(rust, "bump_merge")


def test_a_name_captured_inside_an_arrow_still_clones(rust):
    assert "move || { bump(t.clone(), 1i64).count }" in _body(rust, "bump_later")


def test_only_the_return_moves_an_earlier_use_clones(rust):
    body = _body(rust, "bump_after_use")
    assert "let first = bump(t.clone(), 2i64);" in body
    assert "return merge(first, t);" in body


def test_a_name_reused_across_one_return_clones_each_use(rust):
    body = _body(rust, "tally_report")
    assert body.count("(t.clone())") == 4, body
