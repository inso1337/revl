"""`revl recover --composition` replays only the calls a composition declares
as inverses and compensations (issue #3149).

The runtime resolves a WAL descriptor's `method` to any callable the emitted
module binds. Before this fix an edited descriptor could make recovery call a
forward emission extern with arguments of the editor's choosing: the header,
and so the composition digest check, is untouched by such an edit. The binding
now hands the runtime only a descriptor whose `(receiver, method)` the
composition's IR declares in an `undo` or `compensate` position, with the
`entry` it is registered as. Anything else is residue (`undeclared`), not
called.

The unit tests drive `CompositionWorld` with a stub runtime and need nothing
installed; the end-to-end ones crash a real run and recover it in a fresh
process, and need the pinned cordis fork (without it they SKIP).
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

from revl.compiler import compile_files  # noqa: E402
from revl.recover_binding import (CompositionWorld, declared_inverses,  # noqa: E402
                                  undeclared_call)

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(line: str, ret: str = "None") -> str:
    return (f"    import os\n"
            f"    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


#: `send` crosses and declares `unsend` as its compensation; `file` goes
#: through the required service `tickets` and is compensated by
#: `tickets.withdraw`; `die` is witnessed with `back` as its undo. Then the
#: activation kills the process.
COMPOSITION = f"""
extern emission fn send(t: Str) -> Unit = @py {{
{_host("'send:' + t")}}}
extern emission fn unsend(t: Str) -> Unit = @py {{
{_host("'unsend:' + t")}}}
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
type Gone = {{ at: Str }}
type Down = {{ code: Str }}
extern pure fn back(g: Gone) -> Unit = @py {{
    return None
}}
extern witnessed[proc] fn keep() -> Result[Gone, Down] undo back(result) = @py {{
    return Ok(Gone(at="x"))
}}
extern emission fn crash() -> Unit = @py {{
    import os
    os._exit(3)
}}
component Agent requires tickets: Tickets {{
  emit send("payment-1") compensate unsend("payment-1")
  emit tickets.file("T1") compensate tickets.withdraw("T1")
  emit crash()
}}
"""


@pytest.fixture
def ir(tmp_path):
    (tmp_path / "a.rvl").write_text(COMPOSITION, encoding="utf-8")
    return compile_files([str(tmp_path / "a.rvl")])


def _descriptor(seq: int, entry: str, receiver, method: str, args: list) -> dict:
    return {"record": "discharge-descriptor", "seq": seq, "entry": entry,
            "call": {"receiver": receiver, "method": method, "args": args}}


class _Runtime:
    """Stands in for `runtime`: records what it was handed, runs nothing."""

    def __init__(self) -> None:
        self.handed: list = []

    def replay_descriptors(self, module, wal_path, descriptors, *, services):
        self.handed.extend(d["seq"] for d in descriptors)
        return {d["seq"]: "ran" for d in descriptors}


def _world(ir: dict, runtime: _Runtime) -> CompositionWorld:
    return CompositionWorld(files=["a.rvl"], digest="d", module=object(),
                            runtime=runtime, wal_path="w", services={},
                            booted=[], provider_session=None, cleanup=[],
                            declared=declared_inverses(ir))


def test_the_declared_inverses_are_the_irs_undo_and_compensate_calls(ir):
    declared = declared_inverses(ir)
    assert declared[(None, "unsend")] == {"compensation"}
    assert declared[("tickets", "withdraw")] == {"compensation"}
    assert declared[(None, "back")] == {"transactional"}
    assert declared[("Stream", "close")] == {"transactional"}
    for forward in ((None, "send"), (None, "crash"), ("tickets", "file"),
                    (None, "file_host"), (None, "withdraw_host")):
        assert forward not in declared


def test_a_descriptor_naming_a_forward_extern_is_unresolved_and_not_handed_over(ir):
    runtime = _Runtime()
    outcome = _world(ir, runtime).replay_descriptors([
        _descriptor(1, "compensation", None, "unsend", ["payment-1"]),
        _descriptor(2, "compensation", None, "send", ["FORGED"]),
        _descriptor(3, "compensation", "tickets", "file", ["FORGED"]),
        _descriptor(4, "compensation", "tickets", "withdraw", ["T1"]),
        # a service method spelled as a module binding is a different call
        _descriptor(5, "compensation", None, "withdraw", ["T1"]),
    ])
    assert runtime.handed == [1, 4]
    assert outcome == {1: "ran", 2: "undeclared", 3: "undeclared", 4: "ran",
                       5: "undeclared"}


def test_a_descriptor_whose_entry_and_method_disagree_is_refused(ir):
    runtime = _Runtime()
    outcome = _world(ir, runtime).replay_descriptors([
        _descriptor(1, "transactional", None, "unsend", ["payment-1"]),
        _descriptor(2, "compensation", None, "back", [{"at": "x"}]),
        _descriptor(3, None, None, "unsend", ["payment-1"]),
        _descriptor(4, "transactional", None, "back", [{"at": "x"}]),
    ])
    assert runtime.handed == [4]
    assert outcome == {1: "undeclared", 2: "undeclared", 3: "undeclared", 4: "ran"}


def test_a_legacy_boundary_inverse_is_checked_by_name_only(ir):
    declared = declared_inverses(ir)
    legacy = {"record": "effect", "seq": 0, "entry": "transactional",
              "call": {"receiver": None, "method": "unsend", "args": ["x"]}}
    assert not undeclared_call(declared, legacy)
    assert not undeclared_call(declared, {**legacy, "call": {
        "receiver": "Stream", "method": "close", "args": ["orders"]}})
    assert undeclared_call(declared, {**legacy, "call": {
        "receiver": None, "method": "send", "args": ["x"]}})


# ------------------------------------------------------------ end to end


def _revl(args: list, cwd: Path, log: Path) -> subprocess.CompletedProcess:
    code = ("import sys\nfrom revl.__main__ import main\n"
            f"sys.exit(main({args!r}))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    env = dict(os.environ, REVL_R_LOG=str(log), PYTHONPATH=path)
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=600)


def _lines(path: Path) -> list:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
def crashed(tmp_path):
    (tmp_path / "a.rvl").write_text(COMPOSITION, encoding="utf-8")
    log = tmp_path / "world.log"
    proc = _revl(["run", "a.rvl", "--wal", "run.wal"], tmp_path, log)
    assert proc.returncode == 3, proc.stderr[-3000:]
    assert _lines(log) == ["send:payment-1", "file:T1"]
    return tmp_path


def _edit(wal: Path, method: str, change: dict) -> None:
    lines = []
    for line in wal.read_text(encoding="utf-8").splitlines():
        record = json.loads(line) if line.strip() else None
        if record and record.get("record") == "discharge-descriptor" \
                and record["call"]["method"] == method:
            for key, value in change.items():
                if key == "call":
                    record["call"].update(value)
                else:
                    record[key] = value
            line = json.dumps(record, sort_keys=True)
        lines.append(line)
    wal.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _recover(run: Path) -> dict:
    proc = _revl(["recover", "--wal", "run.wal", "--composition", "a.rvl",
                  "--json"], run, run / "world.log")
    assert proc.stdout.strip(), proc.stderr[-3000:]
    return json.loads(proc.stdout)


@needs_cordis
def test_an_honest_recover_still_runs_both_compensations(crashed):
    report = _recover(crashed)
    assert _lines(crashed / "world.log")[2:] == ["withdraw:T1", "unsend:payment-1"]
    assert sorted(e["referent"] for e in report["compensationsRan"]) == [
        "tickets.withdraw('T1')", "unsend('payment-1')"]


@needs_cordis
def test_a_forged_descriptor_naming_the_forward_send_is_never_called(crashed):
    _edit(crashed / "run.wal", "unsend",
          {"call": {"method": "send", "args": ["FORGED-PAYMENT-999"]}})
    report = _recover(crashed)
    # the forged line ran nothing; the honest one beside it still ran
    assert _lines(crashed / "world.log")[2:] == ["withdraw:T1"]
    [refused] = [r for r in report["residue"]["outstanding"]
                 if r["referent"] == "send('FORGED-PAYMENT-999')"]
    assert (refused["kind"], refused["error"]["type"]) == (
        "unresolved-residue", "undeclared")
    assert "not declared as an inverse or compensation of this entry kind" \
        in refused["error"]["message"]
    assert report["residue"]["clean"] is False


@needs_cordis
def test_a_forged_service_descriptor_naming_a_forward_method_is_never_called(crashed):
    _edit(crashed / "run.wal", "withdraw", {"call": {"method": "file",
                                                     "args": ["FORGED"]}})
    _recover(crashed)
    assert _lines(crashed / "world.log")[2:] == ["unsend:payment-1"]


@needs_cordis
def test_a_compensation_recorded_as_a_transactional_inverse_is_refused(crashed):
    _edit(crashed / "run.wal", "unsend", {"entry": "transactional"})
    report = _recover(crashed)
    assert _lines(crashed / "world.log")[2:] == ["withdraw:T1"]
    assert "unsend('payment-1')" in {
        r["referent"] for r in report["residue"]["outstanding"]
        if r["kind"] == "unresolved-residue"}
