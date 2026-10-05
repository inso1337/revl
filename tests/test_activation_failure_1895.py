"""A component whose activation raises is reported as that, with its error.

Issue #1895. On the py tier a lifecycle test whose component's activation
raised (here a host body whose import fails) failed at its first `call` with
"no provider for key 'box' ... a component with an unmet `requires` stays
PENDING (R2)", for a component that requires nothing, and the raised
`ModuleNotFoundError` appeared nowhere. cordis lands such a fiber FAILED with
the error recorded on it; nothing read it. The ts, go, rust and java runners
all surface the raised error.

The fix reads it in two places:
- the py lifecycle runner's `_revl_call`: when a key has no provider and its
  provider (from the emitted `_REVL_PROVIDERS` table) is a loaded fiber whose
  activation raised, it names that provider and the error, chained. The load
  itself still settles quietly, because a lifecycle test may load a component
  whose activation fails on purpose and assert what it left behind
  (tests/test_lifecycle_refused_open_1859.py);
- the session reports it on the FAILED component's `state()` row, and a call
  on a key whose provider failed names that provider and the error.

The R2 sentence stays for a `requires` that is genuinely unmet.
"""

import copy
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load_by_path import load_by_path  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the lifecycle runner and the session drive a live cordis-py "
           "composition; install it with `sh backends/python/setup.sh` and run "
           "under its venv",
)

_PROVIDER = """
type BoxHandle = Opaque

service Box { fn get() -> Int }

extern acquire fn box_open() -> BoxHandle undo box_close(result)
  = @py { from no_such_module_xyz import thing; return 1 }

extern pure fn box_close(h: BoxHandle) -> Unit = @py { return None }

component BoxKit provides box: Box {
  let h = effect box_open() undo box_close(h)
  provide box { fn get() = 1 }
}
"""

_RAISING = _PROVIDER + """
lifecycle test "activation that raises" {
  load BoxKit
  let v = call box.get()
  assert v == 1
}
"""

# a consumer whose `requires` nobody provides: the R2 case proper
_UNMET = """
service Box { fn get() -> Int }
service Front { fn read() -> Int }

component Shelf requires box: Box provides front: Front {
  provide front { fn read() = box.get() }
}

lifecycle test "a requires nobody provides" {
  load Shelf
  let v = call front.read()
  assert v == 1
}
"""


def _run_py(source: str, tmp_path: Path) -> str:
    """`revl test --backend py` on `source`, as an author runs it."""
    path = tmp_path / "t.rvl"
    path.write_text(source, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_ROOT / "src"), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-P", "-m", "revl", "test", str(path), "--backend", "py"],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=300)
    return result.stdout + result.stderr


def test_the_emitted_harness_knows_each_keys_providers():
    # static, so it runs without cordis
    emit = load_by_path("emit_py_1895", _ROOT / "backends" / "python" / "emit.py")
    text = emit.emit(compile_source(_RAISING, "t.rvl"))
    assert "_REVL_PROVIDERS = {'box': ['BoxKit']}" in text
    assert "        _revl_fibers = _revl_live()" in text
    assert "its provider {} failed to activate" in text


@needs_cordis
def test_a_raising_activation_is_reported_with_its_error(tmp_path):
    out = _run_py(_RAISING, tmp_path)
    assert ("no provider for key 'box': its provider BoxKit failed to "
            "activate: ModuleNotFoundError: No module named 'no_such_module_xyz'") in out, out
    assert "(R2)" not in out, out


@needs_cordis
def test_a_genuinely_unmet_requires_keeps_the_r2_message(tmp_path):
    out = _run_py(_UNMET, tmp_path)
    assert "no provider for key 'front'" in out, out
    assert "stays PENDING (R2)" in out, out
    assert "failed to activate" not in out, out


@needs_cordis
def test_the_session_names_the_failed_provider_and_its_error():
    from revl.mcp.session import Session, SessionError
    session = Session()
    try:
        loaded = session.load(copy.deepcopy(compile_source(_PROVIDER, "t.rvl")))
        (row,) = loaded["components"]
        assert row["state"] == "FAILED"
        assert row["error"] == {"type": "ModuleNotFoundError",
                                "message": "No module named 'no_such_module_xyz'"}
        with pytest.raises(SessionError) as excinfo:
            session.call("box", "get", [])
        assert str(excinfo.value) == (
            "key 'box' has no provider: its provider `BoxKit` failed to "
            "activate: ModuleNotFoundError: No module named 'no_such_module_xyz'")
    finally:
        session.unload()


@needs_cordis
def test_an_active_component_row_carries_no_error_key():
    from revl.mcp.session import Session
    source = _PROVIDER.replace(
        "= @py { from no_such_module_xyz import thing; return 1 }",
        "= @py { return 1 }")
    session = Session()
    try:
        (row,) = session.load(copy.deepcopy(compile_source(source, "t.rvl")))["components"]
        assert row == {"name": "BoxKit", "state": "ACTIVE"}
    finally:
        session.unload()
