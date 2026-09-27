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
    # the two compensated emissions are OFFSET once their compensations ran
    # (the descriptor's `offsets` link, #1369); the bare `crash` emission has
    # no compensation and is the only residue left
    assert sorted(e["label"] for e in report["offset"]) == ["note", "tickets.file"]
    assert [e["label"] for e in report["unreconstructible"]] == ["crash"]
    assert [r["kind"] for r in report["residue"]["outstanding"]] == ["unreconstructible"]
    assert report["residue"]["clean"] is False
    assert proc.returncode == 1


#: The same composition, but the process dies inside a WITNESSED effect, not
#: an emission: `die` exits before it returns, so nothing about it crossed and
#: no inverse was registered. Every crossing the WAL records is compensated.
WITNESSED_CRASH = COMPOSITION.replace(
    """extern emission fn crash() -> Unit = @py {
    import os
    os._exit(3)
}""", """type Gone = { at: Str }
type Down = { code: Str }
extern pure fn back(g: Gone) -> Unit = @py {
    return None
}
extern witnessed[proc] fn die() -> Result[Gone, Down] undo back(result) = @py {
    import os
    os._exit(3)
}""").replace("  emit crash()\n", "  effect die()\n")


@needs_cordis
def test_a_real_recover_of_a_fully_compensated_crash_is_clean(tmp_path):
    """Every emission the crashed activation made has a declared
    compensation, and the crash is not itself a crossing. A real recover runs
    both compensations and gives a CLEAN verdict: exit 0, `world: "real"`, and
    calls made against it."""
    assert "effect die()" in WITNESSED_CRASH and "emit crash()" not in WITNESSED_CRASH
    (tmp_path / "agent.rvl").write_text(WITNESSED_CRASH, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "agent.rvl", "--wal", "run.wal"], tmp_path, log)
    assert proc.returncode == 3, proc.stderr[-3000:]
    state = {"dir": tmp_path, "log": log, "wal": tmp_path / "run.wal"}

    proc, report = _recover(state)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert log.read_text(encoding="utf-8").splitlines() == [
        "note:boot", "file:T1", "withdraw:T1", "offset:boot"]
    assert report["world"] == "real"
    assert report["worldCalls"] > 0
    assert report["residue"]["clean"] is True
    assert report["residue"]["outstanding"] == []
    assert sorted(e["label"] for e in report["offset"]) == ["note", "tickets.file"]
    assert report["unreconstructible"] == []


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
    assert ("agent.rvl is not the composition that wrote generation 1 "
            "(opening 1 of this log, from seq 0)") in proc.stderr
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


def _legacy_wal(path: Path, op: dict, *, ir: dict | None = None) -> str:
    wal = replay.WriteAheadLog(str(path), ir=ir or {}, generation=1).open()
    wal.record_boundary("Store", "scratch", resource="File", inverse_op=op)
    wal.close()
    return str(path)


def test_a_legacy_boundary_inverse_goes_to_the_binding_as_a_transactional_entry(tmp_path):
    """A `record_boundary` inverse is a named call with captured arguments, so
    the binding replays it as the transactional entry it is, in the same batch
    as the descriptors. Recovery writes no fence of its own for it: the fence
    and the `aborted` record are the runtime's."""
    path = _legacy_wal(tmp_path / "legacy.wal",
                       {"receiver": None, "method": "offset", "args": ["x"]})
    before = Path(path).read_text(encoding="utf-8")
    world = _StubBinding({0: "ran"})

    report = recover(path, world=world)

    assert world.handed == [0]
    [entry] = report["ran"]
    assert (entry["label"], entry["replay"], entry["op"]["method"]) == (
        "scratch", "binding", "offset")
    assert report["residue"]["clean"] is True
    assert Path(path).read_text(encoding="utf-8") == before


def test_a_legacy_inverse_the_binding_cannot_resolve_is_residue_by_name(tmp_path):
    """The one py-tier writer of a reconstructible legacy inverse is a durable
    cursor subscription, `Stream.close(cursor)`. `Stream` is not a module
    binding or a required-service key, so the runtime answers `unresolved`, and
    the verdict names the call against its `effect` record."""
    path = _legacy_wal(tmp_path / "cursor.wal",
                       {"receiver": "Stream", "method": "close", "args": ["orders"]})
    report = recover(path, world=_StubBinding({0: "unresolved"}))

    assert report["ran"] == []
    [rec] = report["residue"]["outstanding"]
    assert (rec["kind"], rec["referent"]) == ("unresolved-residue",
                                              "Stream.close('orders')")
    assert (rec["crossing"]["key"], rec["crossing"]["method"]) == ("Store", "scratch")


@needs_cordis
def test_a_real_recover_replays_a_legacy_inverse_through_the_runtime(tmp_path):
    """End to end: a legacy inverse naming one of the composition's own
    externs runs through `runtime.replay_descriptors`, and the runtime's
    `aborted` record settles it."""
    from revl.compiler import compile_files  # noqa: PLC0415
    (tmp_path / "agent.rvl").write_text(COMPOSITION, encoding="utf-8")
    ir = compile_files([str(tmp_path / "agent.rvl")])
    _legacy_wal(tmp_path / "run.wal",
                {"receiver": None, "method": "offset", "args": ["legacy"]}, ir=ir)
    state = {"dir": tmp_path, "log": tmp_path / "world.log",
             "wal": tmp_path / "run.wal"}

    proc, report = _recover(state)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert state["log"].read_text(encoding="utf-8").splitlines() == ["offset:legacy"]
    assert [(e["label"], e["replay"]) for e in report["ran"]] == [("scratch", "binding")]
    assert report["binding"]["booted"] == []
    [aborted] = [r for r in _records(state["wal"]) if r["record"] == "aborted"]
    assert aborted["replayed"] == [0]


def test_an_owed_emission_is_not_re_fired_and_the_verdict_says_why(tmp_path):
    """No runtime entry point re-fires a deferred emission in a fresh process,
    so the binding does not; the verdict names what the runtime would need, and
    no `reissue-fence` is spent."""
    path = tmp_path / "owed.wal"
    wal = replay.WriteAheadLog(str(path), ir={}, generation=1).open()
    wal.record_deferred_emission(receiver="ledger", method="post", args=["k1"],
                                 register="keyed", idempotency="k1")
    wal.record_commit_approved("h0")
    wal.close()
    before = path.read_text(encoding="utf-8")

    report = recover(str(path), world=_StubBinding({}), reissue="keyed")

    assert report["reissued"] == []
    [rec] = report["residue"]["outstanding"]
    assert rec["kind"] == "unbound-residue"
    assert "no fresh-process entry point that re-fires" in rec["error"]["message"]
    assert "reissue-fence" not in path.read_text(encoding="utf-8")[len(before):]


def test_a_shared_reclaim_is_not_attempted_and_the_verdict_says_why(tmp_path):
    """A shared grant is fenced by handle and the runtime's replay path by
    seq; the binding does not invent a seq, spends no fence, and says so."""
    from revl.wal import WAL_VERSION  # noqa: PLC0415
    path = tmp_path / "shared.wal"
    path.write_text(
        json.dumps({"record": "header", "walVersion": WAL_VERSION,
                    "generation": 1, "guarantee": "g"}) + "\n"
        + json.dumps({"record": "shared-grant", "handle": "h1",
                      "inverse": {"receiver": None, "method": "close",
                                  "args": ["h1"]},
                      "holders": ["a"]}) + "\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    report = recover(str(path), world=_StubBinding({}))

    [reclaim] = report["shared"]["reclaims"]
    assert reclaim["ok"] is False
    assert "fenced by HANDLE" in reclaim["error"]["message"]
    assert path.read_text(encoding="utf-8") == before


# ------------------------------------------------ a WAL reused across generations
#
# A log is reopened by a `--watch` reload and by a later run reusing the file
# (#641/#642). The header names only the first opening's composition, so every
# later opening records its own (`generation` record: generation, fromSeq,
# composition), and recover binds each opening's calls to its own composition.


def _composition(tmp_path: Path, name: str, tag: str) -> Path:
    path = tmp_path / name
    path.write_text(COMPOSITION.replace('offset("boot")', f'offset("{tag}")')
                    .replace('"T1"', f'"T-{tag}"'), encoding="utf-8")
    return path


def test_a_reopened_wal_records_each_openings_composition(tmp_path):
    """The #641/#642 reuse path: run 1 opens the log, run 2 reopens it with a
    different composition, a bare reopen (no IR) records nothing new."""
    from revl.compiler import compile_files  # noqa: PLC0415
    ir_a = compile_files([str(_composition(tmp_path, "a.rvl", "a"))])
    ir_b = compile_files([str(_composition(tmp_path, "b.rvl", "b"))])
    path = tmp_path / "reused.wal"
    wal = replay.WriteAheadLog(str(path), ir=ir_a, generation=1).open()
    wal.record_discharge_descriptor("compensation", receiver=None,
                                    method="offset", args=["a"])
    wal.close()
    wal = replay.WriteAheadLog(str(path), ir=ir_b, generation=2).open()
    wal.record_discharge_descriptor("compensation", receiver=None,
                                    method="offset", args=["b"])
    wal.close()
    replay.WriteAheadLog(str(path), ir={}, generation=3).open().close()

    records = _records(path)
    assert records[0]["composition"] == replay.composition_digest(ir_a)
    [opening] = [r for r in records if r["record"] == "generation"]
    assert opening == {"record": "generation", "generation": 2, "fromSeq": 1,
                       "composition": replay.composition_digest(ir_b)}


def test_recover_binds_each_generations_calls_to_its_own_composition(tmp_path):
    from revl.recover_binding import CompositionMismatch, plan_generations  # noqa: PLC0415
    from revl.wal import read_wal  # noqa: PLC0415
    path = tmp_path / "reused.wal"
    wal = replay.WriteAheadLog(str(path), ir={"x": 1}, generation=1).open()
    wal.record_discharge_descriptor("compensation", receiver=None,
                                    method="offset", args=["a"])     # seq 0
    wal.close()
    wal = replay.WriteAheadLog(str(path), ir={"x": 2}, generation=2).open()
    wal.record_discharge_descriptor("compensation", receiver=None,
                                    method="offset", args=["b"])     # seq 1
    wal.close()
    log = read_wal(str(path))
    a = replay.composition_digest({"x": 1})
    b = replay.composition_digest({"x": 2})

    assert [s["generation"] for s in plan_generations(log, a, ["a.rvl"]).values()] == [2]
    assert list(plan_generations(log, a, ["a.rvl"])) == [1]
    assert list(plan_generations(log, b, ["b.rvl"])) == [0]
    with pytest.raises(CompositionMismatch) as refused:
        plan_generations(log, "sha256:other", ["c.rvl"])
    message = str(refused.value)
    assert "generation 1 (opening 1 of this log, from seq 0)" in message
    assert "generation 2 (opening 2 of this log, from seq 1)" in message


@needs_cordis
def test_two_runs_reusing_one_wal_recover_only_with_their_own_compositions(tmp_path):
    """Two real runs of DIFFERENT compositions crash into the same WAL. Each
    recover replays only the calls its own composition wrote, names the other
    generation's calls as residue, and a composition that wrote neither is
    refused naming both generations."""
    for name, tag in (("a.rvl", "a"), ("b.rvl", "b"), ("c.rvl", "c")):
        _composition(tmp_path, name, tag)
    log = tmp_path / "world.log"
    for name in ("a.rvl", "b.rvl"):
        proc = _revl(["run", name, "--wal", "run.wal"], tmp_path, log)
        assert proc.returncode == 3, proc.stderr[-3000:]
    crashed = log.read_text(encoding="utf-8").splitlines()
    assert crashed == ["note:boot", "file:T-a", "note:boot", "file:T-b"]

    def recover_with(name: str) -> tuple:
        proc = _revl(["recover", "--wal", "run.wal", "--composition", name,
                      "--json"], tmp_path, log)
        return proc, (json.loads(proc.stdout) if proc.stdout.strip() else None)

    proc, report = recover_with("c.rvl")
    assert report is None and proc.returncode == 1
    assert "opening 1 of this log" in proc.stderr
    assert "opening 2 of this log" in proc.stderr

    proc, report = recover_with("a.rvl")
    assert log.read_text(encoding="utf-8").splitlines()[len(crashed):] == [
        "withdraw:T-a", "offset:a"]
    other = [r for r in report["residue"]["outstanding"]
             if r["kind"] == "generation-residue"]
    assert sorted(r["referent"] for r in other) == [
        "offset('b')", "tickets.withdraw('T-b')"]
    assert all("opening 2 of this log" in r["error"]["message"] for r in other)
    assert report["binding"]["otherGenerations"] == [
        "generation 1 (opening 2 of this log, from seq 9)"]

    proc, report = recover_with("b.rvl")
    assert log.read_text(encoding="utf-8").splitlines()[len(crashed):] == [
        "withdraw:T-a", "offset:a", "withdraw:T-b", "offset:b"]
    assert sorted(e["referent"] for e in report["compensationsRan"]) == [
        "offset('b')", "tickets.withdraw('T-b')"]
    assert sorted(e["referent"] for e in report["settledByReplay"]) == [
        "offset('a')", "tickets.withdraw('T-a')"]
    assert not [r for r in report["residue"]["outstanding"]
                if r["kind"] == "generation-residue"]
