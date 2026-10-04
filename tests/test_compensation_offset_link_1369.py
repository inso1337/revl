"""A compensation's WAL descriptor names the emission it offsets, and recovery
counts that emission as offset once the compensation has run (issue #1369,
for the `revl recover` work in issue #1477).

WHAT WAS WRONG. In a real `revl run --wal` log, nothing linked an emission's
`effect` record to its compensation's discharge descriptor. The emission's
record said `compensated: false` for a compensated emission, and the
activation body's compensation was recorded as a generic `effect`, because
the recorder paired a compensation with its emission by the source line of
the object yielded, and an activation body yields the runtime's
`_Compensation` entry, which has no such line. So after the compensations
ran for real, recovery still reported `note` and `tickets.file` as
"closure-only, still out".

WHAT IS WRITTEN NOW. The frame pairs a compensation with the recorded
emission it offsets when it registers it, and the discharge descriptor
carries `offsets`: the seq of that emission's `effect` record. The link sits
on the descriptor and not on the effect record because the effect record is
written ahead of the host body, and the descriptor only after it returns.
The timeline's `compensation` record is paired too.

WHAT RECOVERY READS. An emission is offset exactly when its compensation's
descriptor seq is settled: a `discharge` record or an `aborted` record names
it. A compensation an `aborted` record names is not re-issued. The timeline's
`compensation` record is reported through its descriptor, not a second time
as closure-only. An emission with no compensation is still out.

The crash, the replay and each recover run in a fresh process and need the
pinned cordis fork; without it they SKIP, and a skip is not a pass.
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

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(line: str, ret: str = "None") -> str:
    return ("    import os\n"
            "    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


PROVIDER = f"""
extern emission fn file_host(t: Str) -> Str = @py {{
{_host("'file:' + t", "'id-' + t")}}}
extern emission fn withdraw_host(t: Str) -> Unit = @py {{
{_host("'withdraw:' + t")}}}
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
"""

#: The #1477 lane's scenario: two compensated emissions, one on a module
#: extern and one through a required service, then a bare emission that
#: kills the process mid-activation.
COMPOSITION = PROVIDER + f"""
extern emission fn note(t: Str) -> Unit = @py {{
{_host("'note:' + t")}}}
extern pure fn offset(t: Str) -> Unit = @py {{
{_host("'offset:' + t")}}}
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

#: A fresh process that re-issues the open descriptors through
#: `runtime.replay_descriptors`, with the composition's module loaded but not
#: activated and only the provider booted.
REPLAY = r"""
import json, sys, types
root, source, provider_src, wal = sys.argv[1:5]
sys.path.insert(0, root + "/backends/python")
sys.path.insert(0, root + "/src")
from revl.compiler import compile_source
from revl.mcp.session import Session
sys.path.insert(0, root + "/tests")
from _backend_import import backend_emitter
provider = Session()
provider.load(compile_source(open(provider_src).read(), "desk.rvl"))
runtime = provider._driver.runtime
module = types.ModuleType("replayed_composition")
exec(compile(backend_emitter("python").emit(compile_source(open(source).read(),
     "agent.rvl")), "agent_emitted", "exec"), module.__dict__)
records = [json.loads(line) for line in open(wal) if line.strip()]
descriptors = [r for r in records if r.get("record") == "discharge-descriptor"]
outcome = runtime.replay_descriptors(
    module, wal, descriptors,
    services={"tickets": provider._driver.root.get("tickets")})
print(json.dumps({str(k): v for k, v in outcome.items()}))
"""


def _run(args: list, cwd: Path, log: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, REVL_R_LOG=str(log))
    return subprocess.run([sys.executable, *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=600)


def _records(wal: Path) -> list:
    return [json.loads(line) for line in wal.read_text(encoding="utf-8").splitlines()
            if line.strip()]


@pytest.fixture
def crashed(tmp_path):
    (tmp_path / "agent.rvl").write_text(COMPOSITION, encoding="utf-8")
    (tmp_path / "desk.rvl").write_text(PROVIDER, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _run(["-m", "revl", "run", "agent.rvl", "--wal", "run.wal"], tmp_path, log)
    assert proc.returncode == 3, proc.stderr[-3000:]
    return {"dir": tmp_path, "log": log, "wal": tmp_path / "run.wal"}


def _recover(state) -> dict:
    proc = _run(["-m", "revl", "recover", "--wal", "run.wal", "--json"],
                state["dir"], state["log"])
    return json.loads(proc.stdout)


def _replay(state) -> dict:
    proc = _run(["-c", REPLAY, str(ROOT), "agent.rvl", "desk.rvl", "run.wal"],
                state["dir"], state["log"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.splitlines()[-1])


@needs_cordis
def test_each_compensation_descriptor_names_the_emission_it_offsets(crashed):
    records = _records(crashed["wal"])
    emissions = {r["label"]: r["seq"] for r in records
                 if r.get("record") == "effect" and r.get("kind") == "emission"}
    offsets = {r["call"]["method"]: r.get("offsets") for r in records
               if r.get("record") == "discharge-descriptor"}
    assert offsets == {"offset": emissions["note"],
                       "withdraw": emissions["tickets.file"]}
    # and the timeline pairs its own compensation records with the emissions,
    # instead of recording each as an unnamed effect
    paired = [(r["label"], r["boundary"]["class"]) for r in records
              if r.get("record") == "effect" and r.get("kind") == "compensation"]
    assert paired == [("compensate note", "compensation"),
                      ("compensate tickets.file", "compensation")]


@needs_cordis
def test_before_the_compensations_run_the_emissions_are_still_out(crashed):
    """The control: a descriptor that is not settled offsets nothing."""
    report = _recover(crashed)
    assert sorted(e["label"] for e in report["unreconstructible"]) == [
        "crash", "note", "tickets.file"]
    assert report["offset"] == []
    # the timeline's compensation records are reported once, by their
    # descriptors, and not a second time as closure-only
    assert sorted(e["label"] for e in report["compensationRecordsDescribed"]) == [
        "compensate note", "compensate tickets.file"]


@needs_cordis
def test_after_a_replay_the_compensated_emissions_are_offset(crashed):
    outcome = _replay(crashed)
    assert set(outcome.values()) == {"ran"}
    assert crashed["log"].read_text(encoding="utf-8").splitlines() == [
        "note:boot", "file:T1", "withdraw:T1", "offset:boot"]

    report = _recover(crashed)

    assert sorted(e["label"] for e in report["offset"]) == ["note", "tickets.file"]
    # the emission with no compensation is still out, and is the only residue
    assert [e["label"] for e in report["unreconstructible"]] == ["crash"]
    assert [r["kind"] for r in report["residue"]["outstanding"]] == ["unreconstructible"]
    # the compensations the replay ran are not re-issued
    assert report["compensationsReissued"] == []
    assert sorted(e["seq"] for e in report["compensationsSettled"]) == sorted(
        int(seq) for seq in outcome)
    assert crashed["log"].read_text(encoding="utf-8").splitlines() == [
        "note:boot", "file:T1", "withdraw:T1", "offset:boot"]
