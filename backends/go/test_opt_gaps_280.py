"""Regression: pre-existing cordis-go `Opt` emitter gaps (roadmap item 280).

Four gaps, all reproduced on *plain fns* (not components), each of which used
to emit go that did not compile or did not run correctly:

  1. a bare `None` (returned, annotated, or passed) emitted the undefined
     identifier `None` instead of a typed `RevlNone[T]{}`;
  2. `Some(literal)` erased to `RevlSome[any]`, so a later type-switch on the
     concrete case never matched;
  3. an annotated empty list emitted `[]any{}` instead of the declared
     element type;
  4. a wildcard `match` on a concrete-typed (scalar) scrutinee emitted a
     `x.(type)` switch, which Go rejects on a non-interface ("not an
     interface").

The compile-time half asserts the emitted shapes directly (runs everywhere).
The executable half emits the fixture's `test` blocks as a Go test package and
runs `go test` — the only thing that proves gap 2's type-switch actually
*discriminates* and gap 4's arm actually *evaluates* — and skips honestly when
no go toolchain is present. The fixture needs no stc-go, so the run uses a bare
module with no network dependency.
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURE = HERE / "testdata" / "opt_gaps_280.rvl"

sys.path.insert(0, str(ROOT / "src"))
from revl import compile_source  # noqa: E402


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_go_emit_280", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _emit_go() -> str:
    return emit.emit(compile_source(FIXTURE.read_text(encoding="utf-8")),
                     package="opt_gaps_280")


def test_bare_none_lowers_to_typed_revl_none():
    src = _emit_go()
    # gap 1: never the undefined identifier `None`; always a typed RevlNone,
    # both where it is returned and where it is passed as an argument.
    assert "return None" not in src
    assert "(None," not in src and "None)" not in src
    assert "RevlNone[int64]{}" in src


def test_some_literal_keeps_its_concrete_element_type():
    src = _emit_go()
    # gap 2: the element type is int64, never erased to `any` — that erasure is
    # exactly what defeats the type-switch below.
    assert "RevlSome[int64]" in src
    assert "RevlSome[any]" not in src
    # the binding holds the *interface* type so the switch on it is legal go.
    assert "var o RevlOpt[int64] = RevlSome[int64]{Value: 7}" in src


def test_annotated_empty_list_infers_its_element_type():
    src = _emit_go()
    # gap 3: `[]int64{}`, not `[]any{}`.
    assert "[]int64{}" in src
    assert "[]any{}" not in src


def test_wildcard_match_on_scalar_has_no_type_switch():
    src = _emit_go()
    # gap 4: the scalar `wild(n)` match lowers to its arm directly. Only the
    # interface-typed Opt matches keep a type-switch, so the sole `.(type)`
    # occurrences are on Opt values — never on the bare `int64` scrutinee.
    assert "n.(type)" not in src


def test_go_build_and_test_pass():
    """Definition of done: the four gaps emit go that a real `go test` both
    compiles and runs green (the fixture's `test` blocks assert the values)."""
    if shutil.which("go") is None:
        pytest.skip("go not on PATH")
    src = _emit_go()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "gen_test.go").write_text(src, encoding="utf-8")
        init = subprocess.run(["go", "mod", "init", "opt_gaps_280"],
                              cwd=root, capture_output=True, text=True)
        assert init.returncode == 0, init.stderr
        run = subprocess.run(["go", "test", "./..."],
                             cwd=root, capture_output=True, text=True)
    assert run.returncode == 0, (run.stdout + "\n" + run.stderr)
