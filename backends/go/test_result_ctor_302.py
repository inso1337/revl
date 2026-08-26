"""Regression: go-emitter Result-constructor + empty-list SILENT value
divergence (roadmap item 302, the value-level sibling of item 280).

`revlEq` on the go tier is `reflect.DeepEqual`, which reports two *different*
generic instantiations unequal even when the underlying values match. So a bare
`Ok(x)` / `Err(x)` / `[]` on one side of an equality, erased to
`RevlOk[..any..]` / `RevlErr[any, ..]` / `[]any`, used to compare UNEQUAL to the
other side's concrete instantiation returned from a typed fn - a silent wrong
answer on go while py agreed (worse than a build failure). Item 280 fixed the
Opt/None + empty-list-argument + wildcard-match cases and gave the constructor
element-type recovery machinery; item 302 threads that recovery across an
equality so BOTH operands emit the identical concrete go type.

The compile-time half asserts the emitted shapes directly (runs everywhere).
The executable half emits the fixture's `test` blocks as a go test package and
runs `go test` - the only thing that proves revlEq now returns the same value
go and py compute for equal Result / list values - and skips honestly when no go
toolchain is present. The fixture needs no stc-go, so the run uses a bare module
with no network dependency.
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
FIXTURE = HERE / "testdata" / "result_ctor_302.rvl"

sys.path.insert(0, str(ROOT / "src"))
from revl import compile_source  # noqa: E402


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_go_emit_302", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _emit_go() -> str:
    return emit.emit(compile_source(FIXTURE.read_text(encoding="utf-8")),
                     package="result_ctor_302")


def test_bare_ok_recovers_both_result_type_params():
    src = _emit_go()
    # The RHS `Ok(1)` carries the concrete (int64, string) it recovers from the
    # equality's other operand - never `RevlOk[int64, any]`, which would defeat
    # reflect.DeepEqual against the returned `RevlOk[int64, string]`.
    assert "RevlOk[int64, string]{Value: 1}" in src
    assert "RevlOk[int64, any]" not in src


def test_bare_err_recovers_the_unseen_ok_type_param():
    src = _emit_go()
    # `Err("boom")` knows only its own (string) err side from its argument; the
    # ok side (int64) must be recovered from the concrete operand, never `any`.
    assert 'RevlErr[int64, string]{Value: "boom"}' in src
    assert "RevlErr[any, string]" not in src
    assert "RevlErr[any, any]" not in src


def test_float_result_equality_keeps_float64():
    src = _emit_go()
    assert "RevlOk[float64, string]{Value: float64(1.5)}" in src
    assert 'RevlErr[float64, string]{Value: "bad"}' in src


def test_empty_list_equality_recovers_element_type():
    src = _emit_go()
    # Both operands of the empty-list equalities are the concrete element type -
    # `[]string{}` / `[]int64{}`, never the `[]any{}` that compared unequal.
    assert "revlEq(empty_strs(), []string{})" in src
    assert "revlEq(empty_ints(), []int64{})" in src
    assert "[]any{}" not in src


def test_go_build_and_test_pass():
    """Definition of done: value equality, not just compilation. The fixture's
    `test` blocks assert `Ok/Err/[]` equalities that FAIL on go pre-fix; a green
    `go test` proves revlEq now computes the same result go and py do."""
    if shutil.which("go") is None:
        pytest.skip("go not on PATH")
    src = _emit_go()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "gen_test.go").write_text(src, encoding="utf-8")
        init = subprocess.run(["go", "mod", "init", "result_ctor_302"],
                              cwd=root, capture_output=True, text=True)
        assert init.returncode == 0, init.stderr
        run = subprocess.run(["go", "test", "./..."],
                             cwd=root, capture_output=True, text=True)
    assert run.returncode == 0, (run.stdout + "\n" + run.stderr)
