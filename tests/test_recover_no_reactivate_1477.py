"""`revl recover` never boots a provider whose activation crosses (issue #1477).

To replay a compensation through a required service, recover boots the
component that provides it. Booting a component runs its activation. When that
activation crosses the boundary (an emission, a witnessed effect, an acquire,
an emission method of a service it requires), the crossing is made a second
time, and the WAL already holds the first: recover would break the
exactly-once claim it exists to keep. On the base, a provider whose activation
emits `note("desk")` emitted it again at every real recover, and the verdict
still read CLEAN.

Recover now reads each provider's activation crossings off the IR, with
`revl audit`'s own boundary walk, and does not boot a provider that has any.
A call that needs it is declined by name (`would-reactivate`, a
`reactivation-residue` naming the provider and its crossings); the rest of the
WAL recovers as before. Covered for a single-process WAL and a placement run.
Also here: a declined call spends no `reissue-fence`, and `revl estop
--report` reads a placement index's process WALs.
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

from revl.compiler import compile_files  # noqa: E402
from revl.recover_binding import activation_crossings, reactivation_refusals  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(line: str, ret: str = "None") -> str:
    return (f"    import os\n"
            f"    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


#: Desk provides `tickets` AND makes a compensated emission when it activates.
#: Agent compensates through `tickets`, so recovering Agent's WAL needs Desk.
#: Then `die` SIGKILLs the process group before it returns.
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
component Desk provides tickets: Tickets {{
  emit note("desk") compensate offset("desk")
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
components = ["Desk"]

[processes.agent]
components = ["Agent"]
"""


@pytest.fixture(autouse=True)
def _placement_tmp(monkeypatch):
    """A short TMPDIR for a placement's sockets (AF_UNIX path limit), removed
    afterwards: a killed conductor never removes its own."""
    directory = tempfile.mkdtemp(prefix="rv1477r", dir="/tmp")
    monkeypatch.setenv("TMPDIR", directory)
    yield
    shutil.rmtree(directory, ignore_errors=True)


def _revl(args: list, cwd: Path, log: Path) -> subprocess.CompletedProcess:
    code = ("import sys\nfrom revl.__main__ import main\n"
            f"sys.exit(main({args!r}))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    env = dict(os.environ, REVL_R_LOG=str(log), PYTHONPATH=path)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=600,
                          start_new_session=True)


def _world(log: Path) -> list:
    return log.read_text(encoding="utf-8").splitlines()


def _crash(tmp_path: Path, *placement: str) -> Path:
    (tmp_path / "app.rvl").write_text(COMPOSITION, encoding="utf-8")
    (tmp_path / "p.toml").write_text(PLACEMENT, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "app.rvl", "--wal", "run.wal", *placement], tmp_path, log)
    assert proc.returncode == -9, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert _world(log) == ["note:desk", "note:agent", "file:T1"]
    return log


def _recover(tmp_path: Path, log: Path) -> tuple:
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "app.rvl",
                  "--json"], tmp_path, log)
    assert proc.stdout.strip(), proc.stderr[-3000:]
    return proc, json.loads(proc.stdout)


REFUSED = [{"key": "tickets", "component": "Desk", "crossings": ["note"]}]


def _reactivation_residue(report: dict) -> list:
    return [r for r in report["residue"]["outstanding"]
            if r["kind"] == "reactivation-residue"]


@needs_cordis
def test_recover_does_not_re_run_a_providers_crossing_activation(tmp_path):
    log = _crash(tmp_path)

    proc, report = _recover(tmp_path, log)

    world = _world(log)
    # Desk's activation emission happened once, at the run; recover never
    # booted Desk, so it did not happen again
    assert world.count("note:desk") == 1
    assert "withdraw:T1" not in world
    assert sorted(world[3:]) == ["offset:agent", "offset:desk"]
    assert report["binding"]["booted"] == []
    assert report["binding"]["refused"] == REFUSED
    [residue] = _reactivation_residue(report)
    assert residue["referent"] == "tickets.withdraw('T1')"
    assert "needs Desk" in residue["error"]["message"]
    assert "crosses note" in residue["error"]["message"]
    assert report["residue"]["clean"] is False
    assert proc.returncode == 1

    # a second recover does not re-run it either, and still names the call
    proc, report = _recover(tmp_path, log)
    assert _world(log) == world
    assert len(_reactivation_residue(report)) == 1


@needs_cordis
def test_placement_recover_does_not_re_run_a_providers_crossing_activation(tmp_path):
    log = _crash(tmp_path, "--placement", "p.toml")

    proc, report = _recover(tmp_path, log)

    world = _world(log)
    assert world.count("note:desk") == 1
    assert "withdraw:T1" not in world
    assert sorted(world[3:]) == ["offset:agent", "offset:desk"]
    by_name = {p["process"]: p["report"] for p in report["processes"]}
    assert by_name["agent"]["binding"]["refused"] == REFUSED
    assert by_name["agent"]["binding"]["booted"] == []
    assert by_name["agent"]["binding"]["reached"] == {}
    [residue] = _reactivation_residue(by_name["agent"])
    assert residue["referent"] == "tickets.withdraw('T1')"
    assert [r["process"] for r in report["residue"]["outstanding"]
            if r["kind"] == "reactivation-residue"] == ["agent"]
    assert by_name["desk"]["residue"]["clean"] is True
    assert proc.returncode == 1


@needs_cordis
def test_the_text_verdict_names_the_provider_it_did_not_boot(tmp_path):
    log = _crash(tmp_path)
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "app.rvl"],
                 tmp_path, log)
    assert ("providers booted: none; not booted: Desk (for `tickets`; its "
            "activation crosses note)") in proc.stdout
    assert "RESIDUE  tickets.withdraw('T1') - tickets.withdraw('T1'): reaching " \
           "`tickets` needs Desk" in proc.stdout


# ---------------------------------------------------------------------------
# the activation reading, without a live composition


def _ir(tmp_path: Path, source: str) -> dict:
    (tmp_path / "c.rvl").write_text(source, encoding="utf-8")
    return compile_files([str(tmp_path / "c.rvl")])


def test_an_activation_crossing_is_read_and_a_provided_body_is_not(tmp_path):
    ir = _ir(tmp_path, COMPOSITION)
    # Desk's provided methods emit, but booting Desk does not run them
    assert activation_crossings(ir, "Desk") == ["note"]
    # Agent: a host emission, a service emission, a witnessed effect; its
    # compensations do not run at activation
    assert activation_crossings(ir, "Agent") == ["die", "note", "tickets.file"]


def test_a_provider_with_a_crossing_free_activation_is_booted(tmp_path):
    source = COMPOSITION.replace('  emit note("desk") compensate offset("desk")\n', "")
    ir = _ir(tmp_path, source)
    assert activation_crossings(ir, "Desk") == []
    assert reactivation_refusals(ir, {"tickets"}) == {}


def test_a_crossing_in_what_the_provider_requires_refuses_it_too(tmp_path):
    """Booting Desk boots what Desk requires. A crossing in THAT activation
    is made again just the same, so the key is refused, naming the component
    that crosses."""
    source = COMPOSITION.replace(
        '  emit note("desk") compensate offset("desk")\n', "").replace(
        "component Desk provides tickets: Tickets {",
        'service Ink {\n  fn level() -> Str\n}\n'
        'component Well provides ink: Ink {\n'
        '  emit note("well")\n'
        '  provide ink {\n    fn level() { return "full" }\n  }\n}\n'
        "component Desk requires ink: Ink provides tickets: Tickets {")
    ir = _ir(tmp_path, source)
    assert reactivation_refusals(ir, {"tickets"}) == {
        "tickets": {"component": "Well", "crossings": ["note"]}}


def test_a_declined_call_spends_no_reissue_fence(tmp_path):
    """The reissue seam writes its at-most-once fence before the fire. A call
    the binding declines is never fired, so a fence for it would stop the one
    recover that can make it from ever making it."""
    from revl.recovery import World, WORLD_REAL, _reissue_through_binding  # noqa: PLC0415

    class Declining(World):
        kind = WORLD_REAL

        def declined(self, seq, receiver):
            return "would-reactivate"

        def reissue_deferred(self, descriptors):
            return {d["seq"]: "would-reactivate" for d in descriptors}

        def reactivation_note(self, receiver):
            return f"reaching `{receiver}` needs Desk"

    wal = tmp_path / "w.wal"
    wal.write_text("", encoding="utf-8")
    descriptor = {"seq": 3, "call": {"receiver": "tickets", "method": "file",
                                     "args": ["T1"]}}
    result = _reissue_through_binding(str(wal), Declining(), descriptor, 3,
                                      "tickets.file('T1')", "keyed")
    assert result["residue"]["kind"] == "reactivation-residue"
    assert wal.read_text(encoding="utf-8") == ""


def _write(path: Path, records: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_estop_report_reads_every_process_wal_of_a_placement(tmp_path):
    from revl.cli.change import _estop_outstanding  # noqa: PLC0415

    _write(tmp_path / "run.wal", [
        {"record": "placement-index", "placementVersion": 1, "processes": [
            {"name": "desk", "wal": "run.wal.desk", "components": ["Desk"],
             "backend": "py"},
            {"name": "agent", "wal": "run.wal.agent", "components": ["Agent"],
             "backend": "py"}]},
        {"record": "opened", "run": 1}])
    header = {"record": "header", "walVersion": 1, "generation": 1, "guarantee": "g"}

    def owed(seq, receiver, method):
        return {"record": "discharge-descriptor", "seq": seq,
                "entry": "compensation",
                "call": {"receiver": receiver, "method": method, "args": []}}

    _write(tmp_path / "run.wal.desk", [header, owed(1, None, "offset")])
    _write(tmp_path / "run.wal.agent", [header, owed(1, None, "offset"),
                                        owed(4, "tickets", "withdraw")])

    report = _estop_outstanding(str(tmp_path / "run.wal"))

    assert report["known"] is True and report["count"] == 3
    assert [(e["process"], e["seq"], e["method"]) for e in report["entries"]] == [
        ("desk", 1, "offset"), ("agent", 1, "offset"), ("agent", 4, "withdraw")]

    (tmp_path / "run.wal.agent").unlink()
    report = _estop_outstanding(str(tmp_path / "run.wal"))
    assert report["known"] is False and report["count"] == 1
    assert report["note"].startswith("process agent: cannot read WAL")
