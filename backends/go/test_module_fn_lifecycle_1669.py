"""Regression for issue #1669: a module `fn` called from component code in a
document that also carries a `lifecycle test`.

A lifecycle test keeps a document on a live renderer, and `emit` sent every
such document to the live stc-go path, which renders the declared types, the
externs and the components but never the module `fn`s. A provide method calling
`double(n)` therefore emitted a package that did not build: `undefined:
double`. A document with a module fn now takes the combined renderer
(`_emit_v3_combined`, issue #1321), which carries the typed-core functions,
their runtime preambles, the plain `test` blocks and the lifecycle tests in one
package.

The shape half runs everywhere. The executable half builds the emitted package
against the pinned stc-go and runs its tests under `go test`, which is the only
thing that proves the lifecycle test really drives the component through the
module fn.
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
FIXTURE = HERE / "testdata" / "module_fn_lifecycle_1669.rvl"
GO_SUM = HERE / "scenarios" / "go.sum"
STC = "github.com/0xdenny218/stc-go v0.6.1-0.20260818143352-b3d6788a428e"

sys.path.insert(0, str(ROOT / "src"))
from revl import compile_source  # noqa: E402


def _emit_module():
    spec = importlib.util.spec_from_file_location("revl_go_emit_1669", HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


emit = _emit_module()


def _emit_go() -> str:
    return emit.emit(compile_source(FIXTURE.read_text(encoding="utf-8")))


def test_the_module_fns_are_in_the_package_beside_the_components():
    src = _emit_go()
    assert "func double(x int64) int64 {" in src
    assert "func label(n int64) string {" in src
    assert "return double(n)" in src, "the provide method calls the module fn"
    assert "func TestTheModuleFnIsOrdinaryCode(revlT *testing.T)" in src, (
        "the plain `test` block is carried too, not only the lifecycle test")
    assert "LoadC(" in src, "the lifecycle test still drives the live component"


def test_the_emitted_package_builds_and_its_tests_pass():
    go = shutil.which("go")
    if go is None:
        pytest.skip("go not on PATH")
    src = _emit_go()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "gen_test.go").write_text(src, encoding="utf-8")
        (root / "go.mod").write_text(
            "module fn1669\n\ngo 1.25.0\n\nrequire %s\n" % STC, encoding="utf-8")
        shutil.copy(GO_SUM, root / "go.sum")
        run = subprocess.run([go, "test", "-count=1", "-vet=off", "./..."],
                             cwd=root, capture_output=True, text=True, timeout=600)
    if run.returncode != 0 and "no required module provides" in (
            run.stdout + run.stderr):  # pragma: no cover — offline module cache
        pytest.skip("stc-go is not in the local module cache")
    assert run.returncode == 0, (run.stdout + "\n" + run.stderr)
