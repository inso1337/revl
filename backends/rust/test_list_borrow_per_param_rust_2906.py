"""The List borrow fixpoint drops only the parameter that escapes (issue #2906).

`_compute_list_param_borrows` used to drop EVERY borrow candidate of a function
the moment one of them escaped, so a `return acc` made the read-only token list
`ts` beside it owned too, and every caller copied the whole list. Only the
escaping parameter is owned now; its read-only sibling stays a `&[..]` slice.
The self-host port follows byte for byte (tests/test_selfhost_emit_rust.py,
`list_borrow_per_param.rvl`), and that file's cargo build-and-run test builds
the fixture on both emitters.
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

_spec = importlib.util.spec_from_file_location("revl_rust_emit_2906", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

DOC = ROOT / "tests" / "fixtures" / "emit_rust_corpus" / "list_borrow_per_param.rvl"


@pytest.fixture(scope="module")
def rust() -> str:
    return emit.emit(compile_files([str(DOC)]))


def _body(rust: str, name: str) -> str:
    return rust.split(f"fn {name}(")[1].split("\n}")[0]


def test_the_read_only_list_param_borrows_beside_an_escaping_one(rust):
    assert ("fn rest_until_blank(ts: &[String], i: i64, acc: Vec<String>) -> Vec<String>"
            in rust), rust


def test_the_borrowed_param_threads_through_and_the_caller_lends(rust):
    assert "rest_until_blank(ts, " in _body(rust, "rest_until_blank")
    assert "rest_until_blank(&ts, 0i64, " in _body(rust, "count_until_blank")


def test_the_analysis_keeps_only_the_escaping_slot_owned():
    doc = compile_files([str(DOC)])
    borrows = emit._compute_list_param_borrows(doc["functions"])
    assert borrows["rest_until_blank"] == frozenset({0})
    assert borrows["count_until_blank"] == frozenset()
