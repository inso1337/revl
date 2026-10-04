"""`revl recover` of a PLACEMENT run (issue #1477).

`revl run --placement P --wal INDEX` writes one WAL per process and, at INDEX,
an index naming them (`revl.placement_wal`). `revl recover --wal INDEX` finds
every process WAL, recovers each through the composition's binding, consumers
first, and gives one verdict naming each process's residue
(`revl.recover_placement`). These tests pin:

* end to end, a two-process placement killed after compensated emissions in
  both processes: a real recover runs every compensation, including the one an
  Agent in one process declared through the `tickets` service a Desk in the
  other process provides, and is clean; a second recover does nothing;
* an orderly `--once` placement commits every process WAL, and recovers clean
  with no call against the world;
* without a commit, no process stamps `activation-complete` on its own; with
  the index's commit recorded, recover stamps a process that died before it did;
* an index is never read as an empty (and so clean) WAL, a missing process WAL
  is residue named by its process, and `--wal` refuses a process on a tier that
  writes none.

The end-to-end tests need the pinned cordis fork; without it they SKIP, and a
skip is not a pass.
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

from revl import placement_wal  # noqa: E402
from revl.recover_placement import _consumers_first, _reach, recover_index  # noqa: E402
from revl.wal import PlacementIndexNotAWAL, read_wal  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WALs are written by a live cordis-py placement; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(line: str, ret: str = "None") -> str:
    return (f"    import os\n"
            f"    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


#: Two processes. `desk` hosts Desk, which provides `tickets`, and Clerk, whose
#: activation makes a compensated emission. `agent` hosts Agent, which makes a
#: compensated emission of its own and one through `tickets`, compensated
#: through `tickets` too, so its compensation crosses into the desk process.
#: Then `die` SIGKILLs the whole process group (conductor and both processes)
#: before it returns, so nothing about it crossed. Desk's activation is
#: side-effect free: recover boots it to reach `tickets`, and booting a
#: provider runs its activation.
COMPOSITION = f"""
extern emission fn file_host(t: Str) -> Str = @py {{
{_host("'file:' + t", "'id-' + t")}}}
extern emission fn withdraw_host(t: Str) -> Unit = @py {{
{_host("'withdraw:' + t")}}}
service Tickets {{
  emission fn file(t: Str) -> Str
  emission fn withdraw(t: Str)
}}
extern emission fn note(t: Str) -> Unit = @py {{
{_host("'note:' + t")}}}
extern pure fn offset(t: Str) -> Unit = @py {{
{_host("'offset:' + t")}}}
type Gone = {{ at: Str }}
type Down = {{ code: Str }}
extern pure fn back(g: Gone) -> Unit = @py {{
    return None
}}
extern witnessed[proc] fn die() -> Result[Gone, Down] undo back(result) = @py {{
    import os, signal
    os.killpg(os.getpgrp(), signal.SIGKILL)
}}
component Clerk {{
  emit note("clerk") compensate offset("clerk")
}}
component Desk provides tickets: Tickets {{
  provide tickets {{
    fn file(t) {{ return emit file_host(t) }}
    fn withdraw(t) {{ emit withdraw_host(t) }}
  }}
}}
component Agent requires tickets: Tickets {{
  emit note("agent") compensate offset("agent")
  emit tickets.file("T1") compensate tickets.withdraw("T1")
  effect die()
}}
"""

PLACEMENT = """
[processes.desk]
components = ["Desk", "Clerk"]

[processes.agent]
components = ["Agent"]
"""


@pytest.fixture(autouse=True)
def _placement_tmp(monkeypatch):
    """A short TMPDIR for the conductor's placement directory (its sockets must
    fit AF_UNIX's path limit), removed afterwards: a killed conductor never
    removes its own."""
    directory = tempfile.mkdtemp(prefix="rv1477p", dir="/tmp")
    monkeypatch.setenv("TMPDIR", directory)
    yield
    shutil.rmtree(directory, ignore_errors=True)


def _revl(args: list, cwd: Path, log: Path) -> subprocess.CompletedProcess:
    code = ("import sys\nfrom revl.__main__ import main\n"
            f"sys.exit(main({args!r}))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    env = dict(os.environ, REVL_R_LOG=str(log), PYTHONPATH=path)
    # its own session, so the crash's `killpg` takes the conductor and both
    # processes and never this test runner
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=600,
                          start_new_session=True)


def _records(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _world(state) -> list:
    return state["log"].read_text(encoding="utf-8").splitlines()


@pytest.fixture
def crashed(tmp_path):
    """The placement, run with `--wal`, until `die` kills every process."""
    (tmp_path / "app.rvl").write_text(COMPOSITION, encoding="utf-8")
    (tmp_path / "p.toml").write_text(PLACEMENT, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "app.rvl", "--placement", "p.toml", "--wal", "run.wal"],
                 tmp_path, log)
    assert proc.returncode == -9, proc.stdout[-3000:] + proc.stderr[-3000:]
    state = {"dir": tmp_path, "log": log, "index": tmp_path / "run.wal",
             "desk": tmp_path / "run.wal.desk", "agent": tmp_path / "run.wal.agent"}
    assert state["index"].exists(), "the placement wrote no WAL index"
    assert _world(state) == ["note:clerk", "note:agent", "file:T1"]
    return state


def _recover(state, *extra: str) -> tuple:
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "app.rvl",
                  "--json", *extra], state["dir"], state["log"])
    report = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc, report


def _descriptors(path: Path) -> dict:
    return {r["call"]["method"]: r for r in _records(path)
            if r.get("record") == "discharge-descriptor"}


@needs_cordis
def test_a_placement_crash_leaves_an_index_and_one_uncommitted_wal_per_process(crashed):
    [header, opened] = _records(crashed["index"])
    assert header["record"] == "placement-index"
    assert [(p["name"], p["components"], p["wal"]) for p in header["processes"]] == [
        ("desk", ["Desk", "Clerk"], "run.wal.desk"),
        ("agent", ["Agent"], "run.wal.agent")]
    assert opened == {"record": "opened", "run": 1}   # never committed

    desk, agent = read_wal(str(crashed["desk"])), read_wal(str(crashed["agent"]))
    # one composition wrote both, and neither process stamped its own marker:
    # a placement's activation is the whole composition's
    assert desk["header"]["composition"] == agent["header"]["composition"]
    assert desk["complete"] is False and agent["complete"] is False
    assert {m: d["call"]["receiver"] for m, d in _descriptors(crashed["desk"]).items()} \
        == {"offset": None}
    assert {m: d["call"]["receiver"] for m, d in _descriptors(crashed["agent"]).items()} \
        == {"offset": None, "withdraw": "tickets"}


@needs_cordis
def test_a_real_recover_of_a_placement_crash_performs_both_processes_and_is_clean(crashed):
    proc, report = _recover(crashed)

    assert proc.returncode == 0, proc.stderr[-3000:]
    # the outside world: every compensation ran once, consumers first and
    # newest first, the cross-process one through the desk's `tickets`
    assert _world(crashed) == ["note:clerk", "note:agent", "file:T1",
                               "withdraw:T1", "offset:agent", "offset:clerk"]
    assert report["verdict"] == "placement"
    assert report["world"] == "real" and report["worldCalls"] == 3
    assert report["placement"]["order"] == ["agent", "desk"]
    assert report["placement"]["committed"] is False
    assert report["residue"] == {
        "clean": True, "outstanding": [],
        "proof": report["residue"]["proof"]}
    by_name = {p["process"]: p for p in report["processes"]}
    agent, desk = by_name["agent"]["report"], by_name["desk"]["report"]
    assert agent["verdict"] == desk["verdict"] == "rolled-back"
    assert sorted(e["seq"] for e in agent["compensationsRan"]) == sorted(
        d["seq"] for d in _descriptors(crashed["agent"]).values())
    assert [e["seq"] for e in desk["compensationsRan"]] == [
        _descriptors(crashed["desk"])["offset"]["seq"]]
    # the agent's `tickets.withdraw` reached the component that provides
    # `tickets`, which the placement hosted in the desk process
    assert agent["binding"]["reached"] == {
        "tickets": {"component": "Desk", "process": "desk"}}
    assert agent["binding"]["booted"] == ["Desk"]
    assert agent["binding"]["unreached"] == []
    assert desk["binding"]["booted"] == []
    assert sorted(e["label"] for e in agent["offset"]) == ["note", "tickets.file"]
    assert [e["label"] for e in desk["offset"]] == ["note"]
    # issue #1889: the emission Desk made while answering agent's
    # cross-process `tickets.file` names that crossing by process and seq, and
    # is counted with it (offset in agent's WAL), not as desk's own residue
    [nested] = desk["nested"]
    assert nested["label"] == "file_host"
    assert nested["within"]["process"] == "agent"
    assert nested["within"]["label"] == "tickets.file"
    assert "in process agent" in nested["why"]
    # each process's WAL: an `aborted` record settles every descriptor it owed
    for name in ("agent", "desk"):
        [aborted] = [r for r in _records(crashed[name]) if r["record"] == "aborted"]
        assert sorted(aborted["replayed"]) == sorted(
            d["seq"] for d in _descriptors(crashed[name]).values())
    assert "process agent: CLEAN" in report["residue"]["proof"]
    assert "process desk: CLEAN" in report["residue"]["proof"]


@needs_cordis
def test_a_second_real_recover_of_the_placement_does_nothing(crashed):
    _recover(crashed)
    world = _world(crashed)
    wals = {n: crashed[n].read_bytes() for n in ("index", "desk", "agent")}

    proc, report = _recover(crashed)

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _world(crashed) == world
    assert {n: crashed[n].read_bytes() for n in wals} == wals
    assert report["worldCalls"] == 0 and report["residue"]["clean"] is True
    for result in report["processes"]:
        assert result["report"]["compensationsRan"] == []
        assert result["report"]["binding"]["booted"] == []
    assert sum(len(r["report"]["settledByReplay"]) for r in report["processes"]) == 3


@needs_cordis
def test_the_text_verdict_names_each_process(crashed):
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "app.rvl"],
                 crashed["dir"], crashed["log"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "verdict: PLACEMENT (2 processes, recovered in the order agent, desk)" \
        in proc.stdout
    assert "== process agent [Agent]" in proc.stdout
    assert "== process desk [Desk, Clerk]" in proc.stdout
    assert "reached tickets: provided by Desk, hosted by process desk" in proc.stdout


@needs_cordis
def test_an_orderly_placement_commits_every_process_wal(tmp_path):
    """`--once` brings every process UP, so the conductor records the commit
    and each process stamps its WAL; teardown adds `run-complete`. Recover rolls
    every process forward and calls nothing."""
    (tmp_path / "app.rvl").write_text(
        COMPOSITION.replace("  effect die()\n", ""), encoding="utf-8")
    (tmp_path / "p.toml").write_text(PLACEMENT, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "app.rvl", "--placement", "p.toml", "--wal", "ok.wal",
                  "--once"], tmp_path, log)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

    assert [r["record"] for r in _records(tmp_path / "ok.wal")] == [
        "placement-index", "opened", "committed"]
    for name, components in (("desk", ["Desk", "Clerk"]), ("agent", ["Agent"])):
        records = _records(tmp_path / f"ok.wal.{name}")
        [marker] = [r for r in records if r["record"] == "activation-complete"]
        assert sorted(marker["components"]) == sorted(components)
        assert records[-1]["record"] == "run-complete"

    proc = _revl(["recover", "--wal", "ok.wal", "--composition", "app.rvl",
                  "--json"], tmp_path, log)
    report = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert report["placement"]["committed"] is True
    assert [r["report"]["verdict"] for r in report["processes"]] == [
        "rolled-forward", "rolled-forward"]
    assert report["worldCalls"] == 0 and report["residue"]["clean"] is True


# ---------------------------------------------------------------------------
# without a live placement


def _write(path: Path, records: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _index(tmp_path: Path, *tail: dict) -> Path:
    index = tmp_path / "run.wal"
    _write(index, [{"record": "placement-index", "placementVersion": 1,
                    "processes": [
                        {"name": "desk", "wal": "run.wal.desk",
                         "components": ["Desk"], "backend": "py"},
                        {"name": "agent", "wal": "run.wal.agent",
                         "components": ["Agent"], "backend": "py"}]},
                   {"record": "opened", "run": 1}, *tail])
    return index


def _process_wal(path: Path, *tail: dict) -> None:
    _write(path, [{"record": "header", "walVersion": 1, "generation": 1,
                   "guarantee": "g"}, *tail])


def test_an_index_is_refused_as_a_wal_rather_than_read_as_an_empty_one(tmp_path):
    """Read as a WAL, an index has no records, so every WAL reader would call
    a crashed placement clean."""
    index = _index(tmp_path)
    with pytest.raises(PlacementIndexNotAWAL, match="run.wal.desk, run.wal.agent"):
        read_wal(str(index))


def test_a_missing_process_wal_is_residue_named_by_its_process(tmp_path):
    index = _index(tmp_path)
    _process_wal(tmp_path / "run.wal.desk")

    report = recover_index(str(index), placement_wal.read_index(str(index)))

    assert report["residue"]["clean"] is False
    [missing] = report["residue"]["outstanding"]
    assert missing["kind"] == "missing-wal" and missing["process"] == "agent"
    assert "process agent: RESIDUE: its WAL" in report["residue"]["proof"]


def test_a_process_that_never_opened_its_wal_crossed_nothing(tmp_path):
    index = _index(tmp_path)
    _process_wal(tmp_path / "run.wal.desk")
    (tmp_path / "run.wal.agent").write_text("", encoding="utf-8")

    report = recover_index(str(index), placement_wal.read_index(str(index)))

    assert report["residue"]["clean"] is True
    states = {r["process"]: r["state"] for r in report["processes"]}
    assert states == {"desk": "recorded", "agent": "never-opened"}


def test_a_recorded_commit_is_completed_for_a_process_that_died_before_its_marker(tmp_path):
    """The conductor records the run's commit before it tells each process to
    stamp its own marker. A process that died in between rolls forward with
    the rest, not back alone."""
    index = _index(tmp_path, {"record": "committed", "run": 1})
    _process_wal(tmp_path / "run.wal.desk",
                 {"record": "activation-complete", "generation": 1,
                  "components": ["Desk"]})
    _process_wal(tmp_path / "run.wal.agent")

    report = recover_index(str(index), placement_wal.read_index(str(index)))

    by_name = {r["process"]: r for r in report["processes"]}
    assert by_name["agent"]["commitStamped"] is True
    assert by_name["desk"]["commitStamped"] is False
    assert {n: r["report"]["verdict"] for n, r in by_name.items()} == {
        "desk": "rolled-forward", "agent": "rolled-forward"}
    marker = _records(tmp_path / "run.wal.agent")[-1]
    assert marker["record"] == "activation-complete"
    assert marker["components"] == ["Agent"]


def test_an_uncommitted_run_does_not_stamp_any_process(tmp_path):
    index = _index(tmp_path, {"record": "committed", "run": 1},
                   {"record": "opened", "run": 2})
    _process_wal(tmp_path / "run.wal.desk")
    _process_wal(tmp_path / "run.wal.agent")

    report = recover_index(str(index), placement_wal.read_index(str(index)))

    assert report["placement"] == {**report["placement"], "run": 2,
                                   "committed": False}
    assert all(r["commitStamped"] is False for r in report["processes"])
    assert all(r["report"]["verdict"] == "rolled-back" for r in report["processes"])


def test_processes_are_recovered_consumers_first():
    entries = [{"name": "base", "components": ["Store"]},
               {"name": "mid", "components": ["Cache"]},
               {"name": "top", "components": ["Api"]}]
    ir = {"components": [
        {"name": "Store", "provides": {"store": "S"}, "requires": {}},
        {"name": "Cache", "provides": {"cache": "C"}, "requires": {"store": "S"}},
        {"name": "Api", "provides": {}, "requires": {"cache": "C"}}]}
    assert [e["name"] for e in _consumers_first(entries, ir)] == ["top", "mid", "base"]


def test_a_key_no_process_provides_is_named_unreached():
    entries = [{"name": "desk", "components": ["Desk"]},
               {"name": "agent", "components": ["Agent"]}]
    ir = {"components": [{"name": "Desk", "provides": {"tickets": "T"}},
                         {"name": "Agent", "requires": {"tickets": "T",
                                                        "remote": "R"}}]}
    reached, unreached = _reach(ir, entries, {"tickets", "remote"})
    assert reached == {"tickets": {"component": "Desk", "process": "desk"}}
    assert unreached == ["remote"]


def test_wal_refuses_a_process_on_a_tier_that_writes_none():
    problem = placement_wal.wal_problem(
        {"desk": {}, "cache": {}}, {"desk": "py", "cache": "go"}, {})
    assert "process 'cache' runs on the go tier" in problem
    assert placement_wal.wal_problem({"desk": {}}, {"desk": "py"}, {}) is None


def test_an_index_for_other_processes_is_not_reused(tmp_path):
    index = _index(tmp_path)
    specs = {"desk": {}}
    with pytest.raises(placement_wal.PlacementIndexError, match="different processes"):
        placement_wal.arm(str(index), {"desk": {"components": ["Desk"]}},
                          {"desk": "py"}, specs)
