"""Regression for issue #1631: a built-in `Ok`/`Err`/`None` whose type the
argument does not fully say.

The go tier spells every built-in Opt/Result as a generic two-value struct, and
Go cannot leave a type argument open. `_go_v3_construct` takes the type
arguments from the expected type, else from the argument, and whatever neither
says became `any`. Three programs the checker accepts then went wrong on go
only:

- `let r = Ok(1); return r` declared `r` as `RevlResult[int64, any]` in a
  function returning `RevlResult[int64, string]`, and `go build` refused it;
- `Ok(g(1))` with `g: (Int) -> Int` could not type its payload at all, because
  a call through a function VALUE inferred no type;
- `Ok(g(2)) == Ok(2)` compared `RevlResult[any, any]` with
  `RevlResult[int64, any]` through `reflect.DeepEqual`, which answers false
  across two instantiations, where python answers true.

The shape half runs everywhere; the executable half runs the fixture's `test`
blocks under `go test` and skips honestly without a go toolchain.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURE = HERE / "testdata" / "result_erased_1631.rvl"

sys.path.insert(0, str(ROOT / "src"))
from revl import compile_source  # noqa: E402


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_go_emit_1631", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _emit_go() -> str:
    return emit.emit(compile_source(FIXTURE.read_text(encoding="utf-8")),
                     package="result_erased_1631")


def _func(src: str, name: str) -> str:
    head = f"func {name}("
    start = src.index(head)
    return src[start:src.index("\n}\n", start)]


def test_a_returned_binding_takes_the_declared_return():
    src = _emit_go()
    assert "var r RevlResult[int64, string] = RevlResult[int64, string]{OkV: 1, Ok: true}" in _func(src, "ok_lit")
    assert 'var e RevlResult[int64, string] = RevlResult[int64, string]{ErrV: "bad"}' in _func(src, "err_lit")
    assert "var n RevlOpt[string] = RevlOpt[string]{}" in _func(src, "none_lit")


def test_a_call_through_a_function_value_types_the_payload():
    body = _func(_emit_go(), "ok_through")
    assert "var r RevlResult[int64, string] = RevlResult[int64, string]{OkV: g(1), Ok: true}" in body


def test_a_binding_handed_to_a_parameter_takes_its_type():
    body = _func(_emit_go(), "handed_on")
    assert "var r RevlResult[int64, string] = RevlResult[int64, string]{OkV: 1, Ok: true}" in body


def test_both_sides_of_an_equality_are_one_instantiation():
    src = _emit_go()
    assert ("revlEq(RevlResult[int64, any]{OkV: g(2), Ok: true}, "
            "RevlResult[int64, any]{OkV: 2, Ok: true})") in _func(src, "eq_through")
    # the callee here cannot be typed, so the payload comes from the other side
    assert ("revlEq(RevlResult[int64, any]{OkV: fs[0](2), Ok: true}, "
            "RevlResult[int64, any]{OkV: 2, Ok: true})") in _func(src, "eq_list_call")
    assert "RevlResult[any, any]" not in src


def test_go_test_passes():
    """Definition of done: it builds, and the equalities answer what python
    answers."""
    if shutil.which("go") is None:
        pytest.skip("go not on PATH")
    src = _emit_go()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "gen_test.go").write_text(src, encoding="utf-8")
        env = {**os.environ, "GO111MODULE": "on"}
        init = subprocess.run(["go", "mod", "init", "result_erased_1631"],
                              cwd=root, capture_output=True, text=True, env=env)
        assert init.returncode == 0, init.stderr
        run = subprocess.run(["go", "test", "./..."], cwd=root,
                             capture_output=True, text=True, env=env, timeout=300)
    assert run.returncode == 0, (run.stdout + "\n" + run.stderr)
