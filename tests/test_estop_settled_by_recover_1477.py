"""`revl estop` counts a descriptor a recover settled as settled (issue #1477).

`revl estop` reads what a halted session still owes off its WAL: every
`discharge-descriptor` nothing has settled. It used to count only a commit's
`discharge` record as settling one. A real `revl recover` settles what it
replayed with the runtime's `aborted` record (`replayed: [seq...]`), the
record `runtime._settled_seqs` and recover itself read. So after a recover had
run every compensation, `revl estop` still listed each one as owed. These tests
pin the shared reader (`wal.settled_descriptor_seqs`), and, end to end, a
crash, a real recover, then `revl estop`, for a single-process WAL and for a
placement run's index.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.cli.change import _estop_outstanding, _render_estop  # noqa: E402
from revl.wal import settled_descriptor_seqs  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(line: str) -> str:
    return (f"    import os\n"
            f"    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return None\n")


#: Two compensated emissions, the second compensated through a service another
#: component provides, then `die` SIGKILLs the process group before it
#: returns. Desk's activation crosses nothing, so recover may boot it.
COMPOSITION = f"""
extern emission fn note(t: Str) -> Unit = @py {{
{_host("'note:' + t")}}}
extern pure fn offset(t: Str) -> Unit = @py {{
{_host("'offset:' + t")}}}
extern emission fn withdraw_host(t: Str) -> Unit = @py {{
{_host("'withdraw:' + t")}}}
service Tickets {{
  emission fn withdraw(t: Str)
}}
type Gone = {{ at: Str }}
type Down = {{ code: Str }}
extern pure fn back(g: Gone) -> Unit = @py {{
    return None
}}
extern witnessed[proc] fn die() -> Result[Gone, Down] undo back(result) = @py {{
    import os, signal
    os.killpg(os.getpgrp(), signal.SIGKILL)
}}
component Desk provides tickets: Tickets {{
  provide tickets {{
    fn withdraw(t) {{ emit withdraw_host(t) }}
  }}
}}
component Agent requires tickets: Tickets {{
  emit note("agent") compensate offset("agent")
  emit note("T1") compensate tickets.withdraw("T1")
  effect die()
}}
"""

PLACEMENT = """
[processes.desk]
components = ["Desk"]

[processes.agent]
components = ["Agent"]
"""


@pytest.fixture(autouse=True)
def _placement_tmp(monkeypatch):
    """A short TMPDIR for a placement's sockets (AF_UNIX path limit), removed
    afterwards: a killed conductor never removes its own."""
    directory = tempfile.mkdtemp(prefix="rv1477e", dir="/tmp")
    monkeypatch.setenv("TMPDIR", directory)
    yield
    shutil.rmtree(directory, ignore_errors=True)


def _revl(args: list, cwd: Path) -> subprocess.CompletedProcess:
    code = ("import sys\nfrom revl.__main__ import main\n"
            f"sys.exit(main({args!r}))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    env = dict(os.environ, REVL_R_LOG=str(cwd / "world.log"), PYTHONPATH=path)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=600,
                          start_new_session=True)


def _crash_and_recover(tmp_path: Path, *placement: str) -> None:
    (tmp_path / "app.rvl").write_text(COMPOSITION, encoding="utf-8")
    (tmp_path / "p.toml").write_text(PLACEMENT, encoding="utf-8")
    proc = _revl(["run", "app.rvl", "--wal", "run.wal", *placement], tmp_path)
    assert proc.returncode == -9, proc.stdout[-3000:] + proc.stderr[-3000:]
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "app.rvl"],
                 tmp_path)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    world = (tmp_path / "world.log").read_text(encoding="utf-8").splitlines()
    assert world[-2:] == ["withdraw:T1", "offset:agent"]


def _estop(tmp_path: Path) -> dict:
    proc = _revl(["estop", "--wal", "run.wal", "--reason", "after recover",
                  "--json"], tmp_path)
    assert proc.returncode == 1, proc.stderr[-3000:]   # an E-Stop is never clean
    return json.loads(proc.stdout)["outstanding"]


@needs_cordis
def test_estop_after_a_recover_owes_nothing_the_recover_settled(tmp_path):
    _crash_and_recover(tmp_path)

    outstanding = _estop(tmp_path)

    assert outstanding["known"] is True
    assert outstanding["entries"] == [] and outstanding["count"] == 0
    assert outstanding["settled"] == 2


@needs_cordis
def test_estop_after_a_placement_recover_owes_nothing_the_recover_settled(tmp_path):
    _crash_and_recover(tmp_path, "--placement", "p.toml")

    outstanding = _estop(tmp_path)

    assert outstanding["known"] is True
    assert outstanding["entries"] == [] and outstanding["count"] == 0
    assert outstanding["settled"] == 2
    assert {p["process"]: p["settled"] for p in outstanding["processes"]} == {
        "desk": 0, "agent": 2}


# ---------------------------------------------------------------------------
# without a live composition


def _write(path: Path, records: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


HEADER = {"record": "header", "walVersion": 1, "generation": 1, "guarantee": "g"}


def _owed(seq: int, method: str) -> dict:
    return {"record": "discharge-descriptor", "seq": seq, "entry": "compensation",
            "call": {"receiver": None, "method": method, "args": []}}


def test_discharge_and_aborted_both_settle_a_descriptor():
    records = [_owed(1, "a"), _owed(2, "b"), _owed(3, "c"),
               {"record": "discharge", "discharged": [1]},
               {"record": "aborted", "replayed": [2]}]
    assert settled_descriptor_seqs(records) == {1, 2}


def test_estop_lists_only_what_neither_record_settled(tmp_path):
    _write(tmp_path / "run.wal", [
        HEADER, _owed(1, "a"), _owed(2, "b"), _owed(3, "c"),
        {"record": "discharge", "discharged": [1]},
        {"record": "aborted", "replayed": [2]}])

    outstanding = _estop_outstanding(str(tmp_path / "run.wal"))

    assert [e["seq"] for e in outstanding["entries"]] == [3]
    assert outstanding["count"] == 1 and outstanding["settled"] == 2


def test_estop_on_a_placement_index_reads_aborted_in_each_process_wal(tmp_path):
    _write(tmp_path / "run.wal", [
        {"record": "placement-index", "placementVersion": 1, "processes": [
            {"name": "desk", "wal": "run.wal.desk", "components": ["Desk"],
             "backend": "py"},
            {"name": "agent", "wal": "run.wal.agent", "components": ["Agent"],
             "backend": "py"}]},
        {"record": "opened", "run": 1}])
    _write(tmp_path / "run.wal.desk", [HEADER, _owed(1, "a"),
                                       {"record": "aborted", "replayed": [1]}])
    _write(tmp_path / "run.wal.agent", [HEADER, _owed(1, "a"), _owed(4, "b"),
                                        {"record": "aborted", "replayed": [4]}])

    outstanding = _estop_outstanding(str(tmp_path / "run.wal"))

    assert [(e["process"], e["seq"]) for e in outstanding["entries"]] == [("agent", 1)]
    assert outstanding["settled"] == 2


def test_the_text_report_says_why_nothing_is_owed(tmp_path):
    _write(tmp_path / "run.wal", [HEADER, _owed(1, "a"),
                                  {"record": "aborted", "replayed": [1]}])
    text = _render_estop({"latch": "l", "reason": "r", "operator": "o",
                          "outstanding": _estop_outstanding(str(tmp_path / "run.wal"))})
    assert "outstanding (0):" in text
    assert ("(none: 1 registered entry was settled on the WAL, by a commit's "
            "discharge or a recover's replay)") in text
