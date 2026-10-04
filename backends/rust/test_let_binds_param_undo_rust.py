"""A provide-method `let` that binds a parameter before an `effect ... undo`
(issue #1723).

Both rust emitters wrote `let key = k;`, which MOVES the parameter `k`. The
`effect` step that follows clones every parameter into `<p>_undo`
(`let k_undo = k.clone();`), so it read the moved value and cargo refused the
crate (E0382). A bare parameter on the right of a body `let` or assignment is
now cloned; a body local stays a move. The self-host port follows byte for
byte (tests/test_selfhost_emit_rust.py, `comp_let_binds_param_undo.rvl`).

The cargo run skips without cargo; CI's `backend-rust` job has it, and a skip
is not a pass.
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

from revl.compiler import compile_files  # noqa: E402

_spec = importlib.util.spec_from_file_location("revl_rust_emit_1723", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

DOC = ROOT / "tests" / "fixtures" / "emit_rust_corpus" / "comp_let_binds_param_undo.rvl"


def _method(rust: str, name: str) -> str:
    """The body of the provide impl's `name` (not the trait declaration)."""
    match = re.search(r"fn " + name + r"\(&self[^\n]*\{\n(.*?)\n    \}\n", rust, re.S)
    assert match, name
    return match.group(1)


@pytest.fixture(scope="module")
def rust() -> str:
    return emit.emit(compile_files([str(DOC)]))


def test_a_let_of_a_parameter_clones_it(rust):
    body = _method(rust, "put")
    assert "let key = k.clone();" in body
    assert "slot = v.clone();" in body
    # the undo clone of the parameter still reads a live `k`
    assert body.index("let key = k.clone();") < body.index("let k_undo = k.clone();")


def test_a_let_of_a_body_local_stays_a_move(rust):
    body = _method(rust, "again")
    assert "let copy = local;" in body


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo")
def test_the_component_builds_under_cargo(tmp_path):
    source = tmp_path / "let_binds_param.rvl"
    source.write_text(DOC.read_text(encoding="utf-8")
                      + '\ntest "builds" { assert 1 == 1 }\n', encoding="utf-8")
    ran = subprocess.run(
        [sys.executable, "-m", "revl", "test", "--backend", "rust", str(source)],
        capture_output=True, text=True, timeout=1800, cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")))
    out = ran.stdout + ran.stderr
    assert "E0382" not in out, out
    assert ran.returncode == 0, out
    assert "test result: ok. 1 passed" in out, out
