"""A crossing of an extern that declares `compensate` registers its
compensation exactly once, in a provide method, on the python tier.

Two changes registered it, by different routes, and each was right alone:
- #1616 (#1592, #1603): the method writes an explicit
  `_revl_frame.compensation_method(...)` after the fire, or
  `(fire, compensation_method(...))[0]` in value position;
- #1591 (#1369): every such extern was decorated with `declared_crossing`, and
  inside the method's call scope the decorator registered it too.
Merged naively, every crossing registered twice and an abort ran the
compensation twice. Now there is one registrar per crossing: a computer-use
extern (a UI transaction unit, item 522) registers through its decorator, so
a raising crossing still registers its own; every other extern registers at
its site, with the named call a fresh process re-issues.
"""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402
from test_ui_transaction_runtime_1369 import DECLARATIONS, UI_TARGET  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="registration is counted on a live cordis-py frame; install the "
           "pinned fork with `sh backends/python/setup.sh`",
)


def _log_body(line: str, ret: str) -> str:
    return ("@py {\n"
            "    import os\n"
            "    with open(os.environ['REVL_UI_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line!r} + chr(10))\n"
            f"    return {ret}\n}}")


#: a non-UI extern that declares `compensate`, crossed as a statement and in
#: value position
PLAIN = (
    f"extern emission fn put_row(body: Str) -> Int compensate restore_row()\n"
    f"  = {_log_body('put', '1')}\n"
    f"extern pure fn restore_row() = {_log_body('restore', 'None')}\n"
    "service Rows {\n"
    "  emission fn put(m: Str)\n"
    "  emission fn keep(m: Str) -> Int\n"
    "}\n"
    "component Store provides rows: Rows {\n"
    "  provide rows {\n"
    "    fn put(m) { emit put_row(m) }\n"
    "    fn keep(m) {\n"
    "      let n = emit put_row(m)\n"
    "      return n\n"
    "    }\n"
    "  }\n"
    "}\n"
)

#: a computer-use extern that declares `compensate`, in a UI transaction unit
UI = UI_TARGET + DECLARATIONS + """
service Ops { emission fn run(region: Str) -> Int }
component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      let pane = emit read_pane(region)
      let amount = emit locate(pane, "Amount")
      emit type_amount(amount, "10")
      return 1
    }
  }
}
"""


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "ui.log"
    monkeypatch.setenv("REVL_UI_LOG", str(path))
    monkeypatch.delenv("REVL_UI_FAIL", raising=False)

    def lines():
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return lines


def _loaded(source: str):
    from revl.mcp.session import Session
    session = Session()
    session.load(copy.deepcopy(compile_source(source, "once.rvl")))
    ((_name, fiber),) = session._driver.fibers.items()
    return session, session._driver.runtime._frame_for_ctx(fiber.ctx)


@needs_cordis
@pytest.mark.parametrize("method", ["put", "keep"])
def test_a_non_ui_crossing_registers_once_and_an_abort_runs_it_once(log, method):
    session, frame = _loaded(PLAIN)
    session.call("rows", method, ["m"])
    assert [e.method for e in frame._compensations] == ["restore_row"]
    frame.abort()
    session.unload()
    assert log() == ["put", "restore"]


@needs_cordis
def test_a_ui_crossing_registers_once_and_an_abort_runs_it_once(log):
    session, frame = _loaded(UI)
    session.call("ops", "run", ["r"])
    assert [e.method for e in frame._compensations] == ["clear_amount"]
    frame.abort()
    session.unload()
    assert log().count("compensate:clear_amount") == 1


def test_only_a_computer_use_extern_is_decorated():
    from _backend_import import backend_emitter
    plain = backend_emitter("python").emit(compile_source(PLAIN, "p.rvl"))
    ui = backend_emitter("python").emit(compile_source(UI, "u.rvl"))
    assert "_revl_declared_crossing" not in plain
    assert "@_revl_declared_crossing('type_amount', lambda: clear_amount(), ui=True" in ui
    # and the UI extern's site writes no second, explicit registration
    run = ui[ui.index("def run(self"):]
    run = run[:run.index("\n\n")]
    assert "compensation_method(lambda: clear_amount()" not in run
