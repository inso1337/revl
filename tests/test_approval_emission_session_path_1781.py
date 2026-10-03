"""The approval WAL says which spend of a grant fired (issue #1781).

`Session.call` spends an approval (a ticket approval, an item-344 standing
grant or an item-251 distilled rule) durably before the crossing fires, and
writes `approval-emission` for that spend once the crossing returns. Every spend
and emission the session writes carries `use`, the 1-based index of that spend
for its `requestId`, so the spends of one multi-use grant are told apart.
`revl.wal.approval_spends` is the join: a spend with its emission fired, a
spend without one is owed or ambiguous.

The exit test cuts the process after a grant's second spend and before its
fire, then reads the WAL alone.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402
from revl.wal import approval_spends, read_wal  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the spends are made by a live cordis-py session; install it with "
           "`sh backends/python/setup.sh` and run under its venv",
)

# `crash` exits the process from inside the host body: the spend is durable,
# the crossing never returns
SOURCE = (
    "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
    "    import os\n"
    "    if msg == 'crash':\n"
    "        os._exit(9)\n"
    "    if msg == 'raise':\n"
    "        raise RuntimeError('the host body failed')\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('announce:' + msg + '\\n')\n"
    "    return\n"
    "}\n"
    "service Ops { emission fn shout(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }\n"
    "}\n"
)

CRASH_RUN = r'''
import sys
sys.path[:0] = [{src!r}, {backend!r}]
from revl import compile_source
from revl.mcp.session import Session
s = Session()
s._wal_path = {wal!r}
s.approval_policy = "auto"
s.load(compile_source({source!r}, "g.rvl"), record=True)
s.mint_standing_grant(capability="announce", uses=3)
s.call("ops", "shout", [{sink!r}, "one"])
s.call("ops", "shout", [{sink!r}, "crash"])
'''


def _session(tmp_path):
    from revl.mcp.session import Session

    session = Session()
    session._wal_path = str(tmp_path / "session.wal")
    session.approval_policy = "auto"
    session.load(compile_source(SOURCE, "g.rvl"), record=True)
    return session


def _records(path):
    return read_wal(str(path))["records"]


def _approval(records):
    return [{k: r[k] for k in ("record", "requestId", "use") if k in r}
            for r in records
            if r.get("record") in ("approval-consumed", "approval-emission")]


@needs_cordis
def test_a_crash_after_the_second_spend_reads_as_use_1_fired_use_2_owed(tmp_path):
    wal, sink = tmp_path / "session.wal", tmp_path / "sink.log"
    script = CRASH_RUN.format(src=str(ROOT / "src"), backend=str(_BACKEND),
                              wal=str(wal), source=SOURCE, sink=str(sink))
    done = subprocess.run([sys.executable, "-P", "-c", script],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 9, done.stderr
    assert sink.read_text(encoding="utf-8") == "announce:one\n"

    records = _records(wal)
    grant = "grant:1:announce"
    assert _approval(records) == [
        {"record": "approval-consumed", "requestId": grant, "use": 1},
        {"record": "approval-emission", "requestId": grant, "use": 1},
        {"record": "approval-consumed", "requestId": grant, "use": 2},
    ]
    assert approval_spends(records) == [
        {"requestId": grant, "use": 1, "fired": True},
        {"requestId": grant, "use": 2, "fired": False},
    ]


@needs_cordis
def test_a_ticket_approval_spend_is_joined_to_its_emission(tmp_path):
    from revl.mcp.approval import ApprovalRequired

    session = _session(tmp_path)
    try:
        sink = str(tmp_path / "sink.log")
        with pytest.raises(ApprovalRequired) as exc:
            session.call("ops", "shout", [sink, "hi"])
        session.approve_ticket(exc.value.ticket["hash"])
        session.call("ops", "shout", [sink, "hi"])
        records = _records(session._wal_path)
        [spend] = approval_spends(records)
        assert spend == {"requestId": exc.value.ticket["hash"], "use": 1,
                         "fired": True}
        [emission] = [r for r in records if r["record"] == "approval-emission"]
        assert emission["component"] == "Agent"
        assert emission["capability"] == "announce"
    finally:
        session.unload()


@needs_cordis
def test_a_crossing_that_raised_leaves_its_spend_unmatched(tmp_path):
    session = _session(tmp_path)
    try:
        sink = str(tmp_path / "sink.log")
        session.mint_standing_grant(capability="announce", uses=2)
        with pytest.raises(RuntimeError):
            session.call("ops", "shout", [sink, "raise"])
        session.call("ops", "shout", [sink, "two"])
        assert [(s["use"], s["fired"]) for s in
                approval_spends(_records(session._wal_path))] == [
            (1, False), (2, True)]
    finally:
        session.unload()


@needs_cordis
def test_the_use_index_runs_on_across_a_swap(tmp_path):
    session = _session(tmp_path)
    try:
        sink = str(tmp_path / "sink.log")
        session.mint_standing_grant(capability="announce", uses=3)
        session.call("ops", "shout", [sink, "one"])
        session.swap(compile_source(
            SOURCE + "service Ping { fn ping() -> Int }\n"
                     "component Other provides p: Ping { provide p { fn ping() = 1 } }\n",
            "g2.rvl"))
        session.call("ops", "shout", [sink, "two"])
        spends = approval_spends(_records(session._wal_path))
        assert [(s["use"], s["fired"]) for s in spends] == [(1, True), (2, True)]
    finally:
        session.unload()


def test_the_join_reads_a_runtime_path_spend_without_a_use():
    records = [{"record": "approval-consumed", "requestId": "a"},
               {"record": "approval-emission", "requestId": "a",
                "capability": "c", "component": "C"},
               {"record": "approval-consumed", "requestId": "b"}]
    assert approval_spends(records) == [
        {"requestId": "a", "use": None, "fired": True},
        {"requestId": "b", "use": None, "fired": False}]
    assert json.dumps(approval_spends([])) == "[]"
