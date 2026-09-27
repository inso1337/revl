"""`revl recover --composition FILE` replays against the REAL world (issue #1477).

Without `--composition`, recover replays a WAL against an in-memory model and
says so (`world: "model"`, tested in test_crash_recovery.py). With it, recover
compiles FILE, checks it against the composition digest in the WAL header, and
re-issues the WAL's open discharge descriptors through the composition's own
host bodies and providers, by the runtime's own abort path
(`runtime.replay_descriptors`). These tests pin that end to end:

* a process crashes mid-activation after two emissions with declared
  compensations, one on a module extern and one through a required service;
  a real recover runs both compensations (the outside system, a log file the
  host bodies append to, shows them) and the WAL shows them settled;
* a second real recover runs nothing and says the descriptors were settled;
* a composition that did not write the WAL is refused by name, and so is a WAL
  that carries no digest;
* a descriptor whose arguments were not captured, one the binding cannot
  resolve, and one an E-Stop strands are residue, named by their call, never
  clean.

The end-to-end tests run the crash and each recover in a fresh process and need
the pinned cordis fork; without it they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import replay  # noqa: E402
from revl.recovery import World, recover, render  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(name: str, line: str, ret: str = "None") -> str:
    return (f"    import os\n"
            f"    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


#: `Agent` crashes mid-activation (the `crash` host body exits the process)
#: after two emissions with declared compensations: `offset` is a module
#: extern (receiver `None`), `tickets.withdraw` goes through the required
#: service `tickets`, which `Desk` provides.
COMPOSITION = f"""
extern emission fn file_host(t: Str) -> Str = @py {{
{_host("file_host", "'file:' + t", "'id-' + t")}}}
extern emission fn withdraw_host(t: Str) -> Unit = @py {{
{_host("withdraw_host", "'withdraw:' + t")}}}
service Tickets {{
  emission fn file(t: Str) -> Str
  emission fn withdraw(t: Str)
}}
component Desk provides tickets: Tickets {{
  provide tickets {{
    fn file(t) {{ return emit file_host(t) }}
    fn withdraw(t) {{ emit withdraw_host(t) }}
  }}
}}
extern emission fn note(t: Str) -> Unit = @py {{
{_host("note", "'note:' + t")}}}
extern pure fn offset(t: Str) -> Unit = @py {{
{_host("offset", "'offset:' + t")}}}
extern emission fn crash() -> Unit = @py {{
    import os
    os._exit(3)
}}
component Agent requires tickets: Tickets {{
  emit note("boot") compensate offset("boot")
  emit tickets.file("T1") compensate tickets.withdraw("T1")
  emit crash()
}}
"""


def _revl(args: list, cwd: Path, log: Path) -> subprocess.CompletedProcess:
    code = ("import sys\nfrom revl.__main__ import main\n"
            f"sys.exit(main({args!r}))\n")
    # the child imports revl from THIS tree, not whatever an editable install
    # elsewhere points at, so the test measures the code it sits next to
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    env = dict(os.environ, REVL_R_LOG=str(log), PYTHONPATH=path)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=600)


def _records(wal: Path) -> list:
    return [json.loads(line) for line in wal.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.fixture
def crashed(tmp_path):
    """Run the composition with a WAL until its activation kills the process."""
    (tmp_path / "agent.rvl").write_text(COMPOSITION, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "agent.rvl", "--wal", "run.wal"], tmp_path, log)
    assert proc.returncode == 3, proc.stderr[-3000:]
    wal = tmp_path / "run.wal"
    descriptors = {r["call"]["method"]: r["seq"] for r in _records(wal)
                   if r.get("record") == "discharge-descriptor"}
    assert set(descriptors) == {"offset", "withdraw"}
    assert log.read_text(encoding="utf-8").splitlines() == ["note:boot", "file:T1"]
    return {"dir": tmp_path, "log": log, "wal": wal, "seqs": descriptors}


def _recover(state, *extra: str) -> tuple:
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "agent.rvl",
                  "--json", *extra], state["dir"], state["log"])
    report = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc, report


@needs_cordis
def test_a_real_recover_runs_the_declared_compensations_and_settles_them(crashed):
    proc, report = _recover(crashed)
    seqs = crashed["seqs"]

    # the outside system: both compensations ran, newest first, with the
    # arguments captured at registration, the service one on the live provider
    assert crashed["log"].read_text(encoding="utf-8").splitlines() == [
        "note:boot", "file:T1", "withdraw:T1", "offset:boot"]
    assert report["world"] == "real"
    assert report["binding"]["booted"] == ["Desk"]
    assert report["binding"]["composition"] == ["agent.rvl"]
    assert sorted(e["seq"] for e in report["compensationsRan"]) == sorted(seqs.values())
    assert report["compensationsReissued"] == []
    # the WAL: an `aborted` record, written by the runtime, settles both seqs
    [aborted] = [r for r in _records(crashed["wal"]) if r["record"] == "aborted"]
    assert sorted(aborted["replayed"]) == sorted(seqs.values())
    # the bare `crash` emission (and the two forward emissions, which a
    # compensation offsets and never inverts) are still honest residue
    assert report["residue"]["clean"] is False
    assert {r["kind"] for r in report["residue"]["outstanding"]} == {"unreconstructible"}
    assert proc.returncode == 1


@needs_cordis
def test_a_second_real_recover_runs_nothing(crashed):
    _recover(crashed)
    log_after_first = crashed["log"].read_text(encoding="utf-8")
    wal_after_first = crashed["wal"].read_text(encoding="utf-8")

    proc, report = _recover(crashed)

    assert crashed["log"].read_text(encoding="utf-8") == log_after_first
    assert crashed["wal"].read_text(encoding="utf-8") == wal_after_first
    assert report["compensationsRan"] == []
    assert sorted(e["seq"] for e in report["settledByReplay"]) == sorted(
        crashed["seqs"].values())
    # nothing is open, so no provider is booted to serve it
    assert report["binding"]["booted"] == []
    assert report["worldCalls"] == 0
    assert proc.returncode == 1   # the emissions' residue is unchanged


@needs_cordis
def test_a_composition_that_did_not_write_the_wal_is_refused_by_name(crashed):
    (crashed["dir"] / "agent.rvl").write_text(
        COMPOSITION.replace('offset("boot")', 'offset("other")'), encoding="utf-8")
    wal_before = crashed["wal"].read_text(encoding="utf-8")

    proc, report = _recover(crashed)

    assert proc.returncode == 1 and report is None
    assert "agent.rvl is not the composition that wrote this WAL" in proc.stderr
    assert crashed["log"].read_text(encoding="utf-8").splitlines() == [
        "note:boot", "file:T1"]
    assert crashed["wal"].read_text(encoding="utf-8") == wal_before


def test_the_wal_header_names_the_composition_it_was_opened_with(tmp_path):
    from revl.compiler import compile_files  # noqa: PLC0415
    source = tmp_path / "c.rvl"
    source.write_text(COMPOSITION, encoding="utf-8")
    ir = compile_files([str(source)])
    with replay.WriteAheadLog(str(tmp_path / "a.wal"), ir=ir, generation=1):
        pass
    with replay.WriteAheadLog(str(tmp_path / "bare.wal"), ir={}, generation=1):
        pass
    [header] = _records(tmp_path / "a.wal")
    assert header["composition"] == replay.composition_digest(ir)
    # the same document from another working directory digests the same
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert replay.composition_digest(compile_files(["c.rvl"])) == header["composition"]
    finally:
        os.chdir(cwd)
    [bare] = _records(tmp_path / "bare.wal")
    assert "composition" not in bare


def test_a_wal_with_no_composition_digest_is_refused_by_name(tmp_path):
    from revl.recover_binding import CompositionMismatch, check_digest  # noqa: PLC0415
    with pytest.raises(CompositionMismatch, match="carries no composition digest"):
        check_digest({"header": {"walVersion": 1}}, "sha256:x", ["agent.rvl"])


# --------------------------------------------------------------- the outcome mapping


class _StubBinding(World):
    """A real-kind world that answers `replay_descriptors` from a table, so the
    mapping from the runtime's outcomes to the verdict is tested without a
    runtime. Records what it was handed."""

    kind = "real"
    replays_descriptors = True
    re_issues_calls = False

    def __init__(self, outcomes: dict) -> None:
        self.outcomes = outcomes
        self.handed: list = []

    def replay_descriptors(self, descriptors: list) -> dict:
        self.handed.extend(d["seq"] for d in descriptors)
        return {d["seq"]: self.outcomes[d["seq"]] for d in descriptors}

    def seed(self, referent, value=True):
        return None

    def remaining(self):
        return []


def _descriptor_wal(path: Path) -> str:
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    wal.record_discharge_descriptor("compensation", receiver="pay",
                                    method="refund", args=["o1"])      # seq 0
    wal.record_discharge_descriptor("compensation", receiver="pay",
                                    method="void", args=["o2"])        # seq 1
    wal.record_discharge_descriptor("transactional", receiver=None,
                                    method="unstash", args=["w"])      # seq 2
    wal.record_discharge_descriptor("compensation", receiver=None,
                                    method="offset", args=["x"])       # seq 3
    wal.close()
    # a compensation whose argument was itself a call: `args: null`
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"record": "discharge-descriptor", "seq": 4,
                                 "entry": "compensation",
                                 "call": {"receiver": "a", "method": "y",
                                          "args": None}}) + "\n")
    return str(path)


def test_unresolved_stranded_and_uncaptured_descriptors_are_residue_by_name(tmp_path):
    path = _descriptor_wal(tmp_path / "d.wal")
    world = _StubBinding({0: "ran", 1: "unresolved", 2: "stranded", 3: "settled"})

    report = recover(path, world=world)

    # `args: null` is never handed to the runtime: nothing to call it with
    assert sorted(world.handed) == [0, 1, 2, 3]
    assert [e["seq"] for e in report["compensationsRan"]] == [0]
    assert [e["seq"] for e in report["settledByReplay"]] == [3]
    assert report["residue"]["clean"] is False
    kinds = {r["referent"]: (r["kind"], r["error"]["type"])
             for r in report["residue"]["outstanding"]}
    assert kinds == {
        "pay.void('o2')": ("unresolved-residue", "unresolved"),
        "unstash('w')": ("stranded-residue", "stranded"),
        "a.y(<not captured>)": ("unresolved-residue", "args-not-captured"),
    }
    text = render(report)
    assert "a.y(<not captured>)" in text and "pay.void('o2')" in text
    assert "ran      compensation seq 0" in text


def test_a_bound_world_attempts_no_call_it_cannot_make(tmp_path):
    """A legacy boundary inverse is not a descriptor: the binding does not
    re-issue it, spends no fence on it, and reports it as residue."""
    path = tmp_path / "legacy.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    wal.record_boundary("Store", "scratch", resource="File",
                        inverse_op={"receiver": "fs", "method": "unlink",
                                    "args": ["/data/x"]})
    wal.close()
    before = path.read_text(encoding="utf-8")

    report = recover(str(path), world=_StubBinding({}))

    assert report["ran"] == []
    [rec] = report["residue"]["outstanding"]
    assert (rec["kind"], rec["outcome"]) == ("unbound-residue", "not-attempted")
    assert path.read_text(encoding="utf-8") == before
