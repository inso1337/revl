"""The E-Stop watcher of a placed process traces its halt (issue #3380).

`revl._process_runner.run` installs the process's host trace, which prints
every host event as a `host | ...` console line, and starts the `revl-estop`
watcher thread for an idle process. The trace is context-local since #3380,
and a thread starts with an empty context: started before the trace, or
without a copy of the context that holds it, the watcher's halt records its
`estop <reason>` event to nobody, and the operator's console shows the
HALTED inventory but not the host line every other halt prints.
"""

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from revl import _process_runner as runner

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the process runner imports the cordis-py runtime at boot")

ROOT = Path(__file__).resolve().parents[1]


class _Stop(Exception):
    """Ends `run()` once the watcher has started: raised in place of the
    compile, the first thing `run()` does after installing the trace."""


class _EstopExit(Exception):
    """Stand-in for `os._exit`, which would take pytest down with it."""


def test_the_latch_halt_of_an_idle_process_prints_its_host_line(
        tmp_path, monkeypatch, capsys):
    backend = str(ROOT / "backends" / "python")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import runtime as rt  # noqa: PLC0415
    import revl.compiler as compiler  # noqa: PLC0415

    rt.clear_estop()
    rt.arm_estop_latch(None)
    latch = tmp_path / "halt.estop"
    latch.write_text(json.dumps({"reason": "button pressed while idle",
                                 "operator": "ops@example"}), encoding="utf-8")
    exited = threading.Event()

    def _exit(code):
        exited.set()
        raise _EstopExit(code)

    def _no_compile(files):
        raise _Stop()

    monkeypatch.setattr(runner.os, "_exit", _exit)
    monkeypatch.setattr(compiler, "compile_files", _no_compile)
    # the watcher dies on the stand-in's exception; keep that off stderr
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    spec = {"name": "idle", "estopLatch": str(latch), "files": []}
    try:
        with pytest.raises(_Stop):
            asyncio.run(runner.run(spec))
        assert exited.wait(10), "the watcher never engaged the halt"
    finally:
        rt.clear_estop()
        rt.arm_estop_latch(None)
        rt.set_trace(None)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert any(line.startswith("[idle] HALTED ") for line in lines), out
    host = [line for line in lines
            if line.startswith("[idle] host") and "| estop" in line]
    assert host, out
    assert "button pressed while idle" in host[0], out
