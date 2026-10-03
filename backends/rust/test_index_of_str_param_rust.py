"""`xs.indexOf(s)` with a borrowed `Str` parameter as the needle (issue #1672).

A read-only `Str` parameter is borrowed as `&str`. As the needle of the STRING
`indexOf` that is right, but the LIST helper is `revl_index_of(&self, needle:
&T)`, `&String` for a `List[Str]`, and `&s` is `&&str` there (E0308). The
emitter rewrote the needle to `&s.to_string()` only when it typed the receiver
as `List[...]`, so a `let`-bound list literal (typed bare `List`) and a list
literal receiver emitted code cargo refused. Every receiver not known to be a
`Str` takes the rewrite now, and a `Str` receiver keeps the plain borrow.

This lives under backends/rust/ so it runs in the `backend-rust` job, which has
cargo. The emit-level checks run anywhere; the cargo run skips without cargo,
and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_rust_emit_1672", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

PROGRAM = """\
type Bag = { names: List[Str] }

fn names() -> List[Str] = ["a", "b"]

fn in_let(s: Str) -> Int {
  let xs = ["a", "b"]
  return xs.indexOf(s)
}

fn in_literal(s: Str) -> Int = ["a", "b"].indexOf(s)

fn in_slice(xs: List[Str], s: Str) -> Int = xs.slice(1, 2).indexOf(s)

fn in_field(b: Bag, s: Str) -> Int = b.names.indexOf(s)

fn in_call(s: Str) -> Int = names().indexOf(s)

fn in_param(xs: List[Str], s: Str) -> Int = xs.indexOf(s)

fn in_str(t: Str, s: Str) -> Int = t.indexOf(s)

test "indexOf with a Str parameter needle" {
  assert in_let("b") == 1
  assert in_literal("b") == 1
  assert in_slice(["a", "b"], "b") == 0
  assert in_field({names: ["a", "b"]}, "b") == 1
  assert in_call("c") == -1
  assert in_param(["a", "b"], "a") == 0
  assert in_str("abc", "c") == 2
}
"""


def _body(rust: str, fn: str) -> str:
    match = re.search(r"fn " + fn + r"\(.*?\n\}", rust, re.S)
    assert match, fn
    return match.group(0)


@pytest.fixture(scope="module")
def rust() -> str:
    return emit.emit(compile_source(PROGRAM, "index_of.rvl"))


@pytest.mark.parametrize("fn", ["in_let", "in_literal", "in_slice", "in_field",
                                "in_call", "in_param"])
def test_a_list_receiver_takes_an_owned_needle(rust, fn):
    body = _body(rust, fn)
    assert "(s: &str" in body or ", s: &str" in body, body
    assert ".revl_index_of(&s.to_string())" in body, body


def test_a_str_receiver_keeps_the_borrow(rust):
    body = _body(rust, "in_str")
    assert ".revl_index_of(&s)" in body, body


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo")
def test_the_program_compiles_and_answers_under_cargo(tmp_path):
    source = tmp_path / "index_of.rvl"
    source.write_text(PROGRAM, encoding="utf-8")
    ran = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "rust", str(source)],
        capture_output=True, text=True, timeout=1800, cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")))
    out = ran.stdout + ran.stderr
    assert ran.returncode == 0, out
    assert "error[E0308]" not in out, out
    assert "test result: ok. 1 passed" in out, out
