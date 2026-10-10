"""The enclosing-crossing record that reaches the wire must be JSON-safe (#2234).

A call a placement process sends while it is inside a recorded crossing carries
that crossing: `bridge._Client.call` puts `bridge._enclosing_crossing()` in the
request and serialises the request with `json.dumps`. The recorder's `_ENCLOSING`
ref also carries the live `Timeline` and the `Step` behind it (issue #1609, what
`_record_emission_outcome` reads), so the bridge has to project the ref down to
its public keys before spreading it. Spreading it whole put a `Timeline` on the
wire and raised `TypeError: Object of type Timeline is not JSON serializable`
inside the SENDING process: the placement's load reported FAILED, the rest of the
composition never ran, and the process still exited 0.

These tests pin the invariant, not the projection: every value of the record that
reaches serialisation is JSON-safe, the crossing's public identity and its
caller-process name survive, and a placement process whose load makes such a call
comes up — recording the crossing in the provider's own WAL under the documented
four-key shape (docs/crash-recovery.md), `process` included, since recovery folds
a served crossing back into the caller's by that name.

The placement test needs the pinned cordis fork; without it it SKIPs, and a skip
is not a pass.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "backends" / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "backends" / "python"))

import bridge  # noqa: E402
import replay  # noqa: E402

from test_recover_placement_1477 import (  # noqa: E402
    COMPOSITION, PLACEMENT, _records, _revl)

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WALs are written by a live cordis-py placement; install the "
           "pinned fork with `sh backends/python/setup.sh`")

#: the public keys of a crossing, as `replay.Step.within` and `bridge._served_within`
#: name them; `_timeline` and `_step` are the recorder's private ones (issue #1609)
PUBLIC = ("seq", "component", "label", "process")


@pytest.fixture
def caller_process():
    """This process is a placement process while the test runs, as
    `bridge._process_runner` sets it, and not one afterwards."""
    previous = bridge.CALLER_PROCESS
    bridge.CALLER_PROCESS = "agent"
    yield "agent"
    bridge.CALLER_PROCESS = previous


@pytest.fixture
def live_crossing():
    """The `_ENCLOSING` ref exactly as the recorder builds it: the public
    crossing plus the live `Timeline`/`Step` `_record_emission_outcome` reads."""
    timeline = replay.Timeline("Desk")
    step = replay.Step(0, "emission", "file_host", "file_host",
                       {"phase": "activation"})
    step.wal_seq = 3
    ref = replay._crossing_ref(timeline, step)
    assert set(ref) == {"seq", "component", "label", "_timeline", "_step"}
    assert isinstance(ref["_timeline"], replay.Timeline)
    token = replay._ENCLOSING.set(ref)
    yield ref
    replay._ENCLOSING.reset(token)


def test_the_crossing_a_placement_call_sends_is_json_safe(caller_process,
                                                          live_crossing):
    """The `tickets.file` call the Agent makes inside the Desk's `file_host`
    crossing is the one `_Client.call` serialises (bridge.py:899-905)."""
    within = bridge._enclosing_crossing()
    assert within is not None, "the crossing was not named for this process"
    assert within["seq"] == 3
    assert within["component"] == "Desk"
    assert within["label"] == "file_host"
    assert within["process"] == caller_process

    request = {"key": "tickets", "method": "file", "args": ["T1"]}
    request["within"] = within
    assert json.loads(json.dumps(request))["within"] == within
    for key, value in within.items():
        assert isinstance(value, (str, int, float, bool)) or value is None, \
            f"{key} is a live {type(value).__name__}, not a JSON value"

    # the projection is a copy: the recorder's ref still holds what
    # `_record_emission_outcome` reads, so journalling the outcome still works
    assert replay._ENCLOSING.get()["_timeline"] is live_crossing["_timeline"]


@pytest.fixture(autouse=True)
def _placement_tmp(monkeypatch):
    """A short TMPDIR for the conductor's placement directory (its sockets must
    fit AF_UNIX's path limit), removed afterwards: a killed conductor never
    removes its own."""
    directory = tempfile.mkdtemp(prefix="rv2234", dir="/tmp")
    monkeypatch.setenv("TMPDIR", directory)
    yield
    shutil.rmtree(directory, ignore_errors=True)


@needs_cordis
def test_a_placement_whose_load_makes_a_crossing_call_comes_up(tmp_path):
    """The Agent's load makes `tickets.file`, which the Desk process answers:
    the load must come up, the call must cross, and the Desk's own record of it
    must name the crossing and the process it was made in."""
    (tmp_path / "app.rvl").write_text(COMPOSITION, encoding="utf-8")
    (tmp_path / "p.toml").write_text(PLACEMENT, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "app.rvl", "--placement", "p.toml", "--wal", "run.wal"],
                 tmp_path, log)
    out = proc.stdout + proc.stderr
    assert "TypeError" not in out, out[-3000:]
    assert "-> FAILED" not in out, out[-3000:]
    assert log.read_text(encoding="utf-8").splitlines() == \
        ["note:clerk", "note:agent", "file:T1"], \
        "the Agent's activation or its crossing call never ran: " + out[-3000:]

    desk = [r for r in _records(tmp_path / "run.wal.desk")
            if r.get("record") == "effect" and r.get("label") == "file_host"]
    assert desk, "the Desk recorded no file_host emission"
    within = desk[0]["within"]
    assert set(within) == set(PUBLIC)
    assert within["component"] == "Agent"
    assert within["label"] == "tickets.file"
    assert within["process"] == "agent"
    assert isinstance(within["seq"], int) and not isinstance(within["seq"], bool)
    assert (tmp_path / "run.wal.agent").exists(), "no agent process WAL"
