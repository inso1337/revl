"""Issue #1589: an extern-declared `compensate` (item 254) emitted from a timer
body (`every` / `after`) is registered on the py tier, once per firing.

The activation-body `emit` step registers the compensation an emission extern
declares; the timer firing called the extern and registered nothing. So an
abort after a firing never ran the reversal, and the WAL carried no discharge
descriptor for it, while the audit surface counted the crossing as compensated.

A firing closure has no generator to yield into, so the entry goes through
`Frame.compensation_method`, the provide-method form: discharged on a clean
commit, run in Phase 2 of an abort, WAL descriptor written at registration.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="drives a live cordis-py composition; install it with "
           "`sh backends/python/setup.sh`",
)

_EXTERNS = """
extern emission fn put_row(body: Str) -> Int
  compensate restore_row()
  = @py {{
    with open({sink!r}, 'a') as _f: _f.write('put ' + body + chr(10))
    return 1
  }}

extern emission fn restore_row() -> Int
  = @py {{
    with open({sink!r}, 'a') as _f: _f.write('restore' + chr(10))
    return 0
  }}

extern emission fn bare_row(body: Str) -> Int
  = @py {{
    with open({sink!r}, 'a') as _f: _f.write('bare ' + body + chr(10))
    return 1
  }}
"""

# mode -> (timer keyword, clock advance in ms, firings that advance produces)
TIMERS = {
    "every": ("every 10s", 25_000, 2),
    "after": ("after 10s", 15_000, 1),
}


def _source(sink: str, mode: str, call: str = 'put_row("x")') -> str:
    keyword = TIMERS[mode][0]
    return (_EXTERNS.format(sink=sink)
            + f"component Beat {{\n  {keyword} {{ emit {call} }}\n}}\n")


def _emit(source: str) -> str:
    emit = load_by_path("revl_py_emit", _BACKEND / "emit.py")
    return emit.emit(compile_source(source, "timer_comp.rvl"))


def _firing_lines(code: str) -> list:
    """The lines of the `_timer_1` firing closure."""
    lines = code.splitlines()
    start = next(i for i, ln in enumerate(lines) if "def _timer_1():" in ln)
    depth = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= depth:
            break
        body.append(ln.strip())
    return body


def _lines(path: str) -> list:
    if not os.path.exists(path):
        return []
    return Path(path).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "sink.log")


# --- the emitted firing -------------------------------------------------------


@pytest.mark.parametrize("mode", sorted(TIMERS))
def test_the_firing_registers_the_declared_compensation(sink, mode):
    body = _firing_lines(_emit(_source(sink, mode)))
    assert body[-1] == "_revl_frame.compensation_method(lambda: restore_row())"
    assert "put_row" in body[-2]


@pytest.mark.parametrize("mode", sorted(TIMERS))
def test_an_extern_with_no_compensate_registers_nothing(sink, mode):
    body = _firing_lines(_emit(_source(sink, mode, 'bare_row("x")')))
    assert not any("compensation" in ln for ln in body)


# --- the runtime --------------------------------------------------------------


def _run(sink: str, mode: str, record: bool = False):
    import runtime
    from revl.mcp.session import Session

    runtime.Clock.reset()
    session = Session()
    session.load(compile_source(_source(sink, mode), "timer_comp.rvl"),
                 record=record)
    runtime.Clock.advance(TIMERS[mode][1])
    return session


@needs_cordis
@pytest.mark.parametrize("mode", sorted(TIMERS))
def test_an_abort_after_a_firing_runs_the_compensation(sink, mode):
    session = _run(sink, mode)
    firings = TIMERS[mode][2]
    assert _lines(sink) == ["put x"] * firings
    session.abort()
    assert _lines(sink) == ["put x"] * firings + ["restore"] * firings


@needs_cordis
@pytest.mark.parametrize("mode", sorted(TIMERS))
def test_a_clean_commit_discharges_the_compensation(sink, mode):
    session = _run(sink, mode)
    firings = TIMERS[mode][2]
    manifest = session.commit()
    assert session.commit_confirm(manifest["hash"])["committed"] is True
    assert _lines(sink) == ["put x"] * firings


@needs_cordis
@pytest.mark.parametrize("mode", sorted(TIMERS))
def test_each_firing_writes_a_compensation_descriptor_to_the_wal(
        sink, mode, tmp_path, monkeypatch):
    import replay

    wal_path = str(tmp_path / "timer.wal")
    real_instrument = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        self.open_wal(wal_path, generation=1)
        return real_instrument(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)
    session = _run(sink, mode, record=True)
    records = replay.WriteAheadLog.read(wal_path)["records"]
    descriptors = [r for r in records
                   if r["record"] == "discharge-descriptor"
                   and r.get("entry") == "compensation"]
    assert len(descriptors) == TIMERS[mode][2]
    session.abort()
