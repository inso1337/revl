"""WAL discharge descriptors name a call a FRESH process can re-issue, and
`runtime.replay_descriptors` re-issues them (found under issue #1369; the
descriptor half of the `revl recover` work, issue #1477).

WHAT WAS WRONG. Every transactional and compensation entry writes a
discharge descriptor at registration, `call: {"receiver", "method", "args"}`.
The runtime filled it in by guessing: `receiver` was the component, `method`
was the first global name the closure loaded, and `args` was `[]` for a
compensation and `[witness]` for an inverse. So `compensate tickets.withdraw(t)`
was recorded as receiver `Agent`, method `tickets`, args `[]`: nothing a fresh
process could call.

WHAT IS RECORDED NOW. The emitter derives the named call at every one of the
four registration sites (activation and method, transactional and
compensation) and passes it as `call=`; the frame writes it verbatim. A
service call's receiver is the required-service key; an extern's is `None`,
the emitted module's own binding. Arguments are evaluated at registration, an
inverse's against the `Ok` witness. A compensation whose argument is itself a
call records `args: null`, because capturing it would cross at registration.

`replay_descriptors` rebuilds each descriptor as the entry it was, under its
original `seq`, and runs it through the frame's own abort path, so the fences,
the budget and the residue records are the in-process ones.

These tests drive a live cordis-py composition and a fresh subprocess. Without
the pinned `cordis` fork they SKIP, and a skip is not a pass.
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
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the descriptors are written by a live cordis-py composition; "
           "install the pinned fork with `sh backends/python/setup.sh`",
)


def _log(expr: str) -> str:
    return ("    import os\n"
            "    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({expr} + chr(10))\n")


PROVIDER = f"""
extern emission fn file_host(t: Str) -> Str = @py {{
{_log("'file:' + t")}    return 'id-' + t
}}
extern emission fn withdraw_host(t: Str) -> Unit = @py {{
{_log("'withdraw:' + t")}    return None
}}
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

#: One site of each of the four kinds, plus an extern-declared compensation:
#:   activation transactional   `effect stash("act")`
#:   activation compensation    `emit note("boot") compensate offset("boot")`
#:   method transactional       `effect stash(p)`
#:   method compensation        `emit tickets.file(t) compensate tickets.withdraw(t)`
#:   method, extern-declared    `return emit direct(t)`
SOURCE = PROVIDER + f"""
type Stash = {{ path: Str, bak: Str }}
type FsError = {{ code: Str }}
extern pure fn unstash(w: Stash) -> Unit = @py {{
{_log("'unstash:' + os.path.basename(w['path'])")}    os.replace(w['bak'], w['path'])
    return None
}}
extern witnessed[fs] fn stash(p: Str) -> Result[Stash, FsError] undo unstash(result) = @py {{
    import os
    path = os.path.join(os.environ['REVL_R_DIR'], p)
    os.replace(path, path + '.bak')
    return Ok({{'path': path, 'bak': path + '.bak'}})
}}
extern emission fn note(t: Str) -> Unit = @py {{
{_log("'note:' + t")}    return None
}}
extern pure fn offset(t: Str) -> Unit = @py {{
{_log("'offset:' + t")}    return None
}}
extern pure fn undo_direct() -> Unit = @py {{
{_log("'undo_direct'")}    return None
}}
extern emission fn direct(t: Str) -> Str compensate undo_direct() = @py {{
{_log("'direct:' + t")}    return 'd'
}}
service Ops {{ emission fn run(p: Str, t: Str) -> Str }}
component Agent requires tickets: Tickets provides ops: Ops {{
  effect stash("act")
  emit note("boot") compensate offset("boot")
  provide ops {{
    fn run(p, t) {{
      effect stash(p)
      emit tickets.file(t) compensate tickets.withdraw(t)
      return emit direct(t)
    }}
  }}
}}
"""

#: The fresh process. It loads the composition's emitted module WITHOUT
#: activating it, activates only the provider a service-call descriptor needs,
#: and hands both to `replay_descriptors`.
CHILD = r"""
import json, sys, types
root, emitted, provider_ir, wal, descriptors = sys.argv[1:6]
sys.path.insert(0, root + "/backends/python")
sys.path.insert(0, root + "/src")
from revl.mcp.session import Session
provider = Session()
provider.load(json.load(open(provider_ir)))
runtime = provider._driver.runtime
module = types.ModuleType("replayed_composition")
exec(compile(open(emitted).read(), emitted, "exec"), module.__dict__)
outcome = runtime.replay_descriptors(
    module, wal, json.load(open(descriptors)),
    services={"tickets": provider._driver.root.get("tickets")})
print(json.dumps({str(k): v for k, v in outcome.items()}))
"""


def _wal_before_apply(monkeypatch, wal_path: str) -> None:
    import replay
    real = replay.Recorder.instrument

    def _open_then_instrument(self, *args, **kwargs):
        self.open_wal(wal_path, generation=1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(replay.Recorder, "instrument", _open_then_instrument)


def _descriptors(wal_path: str) -> list:
    import replay
    return [r for r in replay.WriteAheadLog.read(wal_path)["records"]
            if r.get("record") == "discharge-descriptor"]


@pytest.fixture
def crashed(tmp_path, monkeypatch):
    """Run one call with a WAL, then stop as a crash would: no verdict, no
    discharge, every descriptor still open."""
    for name in ("act", "work"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    log = tmp_path / "r.log"
    monkeypatch.setenv("REVL_R_LOG", str(log))
    monkeypatch.setenv("REVL_R_DIR", str(tmp_path))
    wal_path = str(tmp_path / "run.wal")
    _wal_before_apply(monkeypatch, wal_path)
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(SOURCE, "replay_1369.rvl"), record=True)
    session.call("ops", "run", ["work", "T1"])
    descriptors = _descriptors(wal_path)
    yield {"tmp": tmp_path, "log": log, "wal": wal_path,
           "descriptors": descriptors, "session": session}
    session._reset()


def _run_child(state) -> dict:
    from _backend_import import backend_emitter
    tmp = state["tmp"]
    emitted = tmp / "composition.py"
    emitted.write_text(backend_emitter("python").emit(
        compile_source(SOURCE, "replay_1369.rvl")), encoding="utf-8")
    provider_ir = tmp / "provider.ir.json"
    provider_ir.write_text(json.dumps(compile_source(PROVIDER, "desk.rvl")),
                           encoding="utf-8")
    descriptors = tmp / "descriptors.json"
    descriptors.write_text(json.dumps(state["descriptors"]), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(ROOT), str(emitted), str(provider_ir),
         state["wal"], str(descriptors)],
        capture_output=True, text=True, timeout=600, env=dict(os.environ))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return {int(k): v for k, v in json.loads(proc.stdout.splitlines()[-1]).items()}


def _calls(descriptors: list) -> list:
    return [(d["entry"], d["call"]["receiver"], d["call"]["method"])
            for d in descriptors]


@needs_cordis
def test_each_site_kind_writes_the_named_call(crashed):
    """The four site kinds, read back off the WAL."""
    calls = {(d["entry"], d["call"]["method"]): d["call"]
             for d in crashed["descriptors"]}
    assert _calls(crashed["descriptors"]) == [
        ("transactional", None, "unstash"),      # activation
        ("compensation", None, "offset"),        # activation
        ("transactional", None, "unstash"),      # method
        ("compensation", "tickets", "withdraw"),  # method, through the service
        ("compensation", None, "undo_direct"),   # method, declared on the extern
    ]
    assert calls[("compensation", "withdraw")]["args"] == ["T1"]
    assert calls[("compensation", "offset")]["args"] == ["boot"]
    assert calls[("compensation", "undo_direct")]["args"] == []
    witnesses = [d["call"]["args"] for d in crashed["descriptors"]
                 if d["entry"] == "transactional"]
    assert [os.path.basename(w[0]["path"]) for w in witnesses] == ["act", "work"]


@needs_cordis
def test_a_fresh_process_re_issues_every_descriptor(crashed):
    """The crash left five descriptors open. A fresh process re-issues all
    five, through the abort's own two phases: both inverses newest first, then
    the three compensations newest first, each with the arguments captured at
    registration, the service call on the live provider."""
    before = crashed["log"].read_text(encoding="utf-8").splitlines()
    outcome = _run_child(crashed)
    seqs = [d["seq"] for d in crashed["descriptors"]]
    assert outcome == {seq: "ran" for seq in seqs}
    replayed = crashed["log"].read_text(encoding="utf-8").splitlines()[len(before):]
    assert replayed == ["unstash:work", "unstash:act",
                        "undo_direct", "withdraw:T1", "offset:boot"]
    tmp = crashed["tmp"]
    assert (tmp / "act").exists() and (tmp / "work").exists()
    assert not (tmp / "act.bak").exists() and not (tmp / "work.bak").exists()


@needs_cordis
def test_a_second_replay_runs_nothing(crashed):
    """The first replay writes its fences and an `aborted` record naming what
    ran; a second one finds every seq settled and crosses nothing."""
    _run_child(crashed)
    after_first = crashed["log"].read_text(encoding="utf-8").splitlines()
    outcome = _run_child(crashed)
    assert set(outcome.values()) == {"settled"}
    assert crashed["log"].read_text(encoding="utf-8").splitlines() == after_first


def test_a_compensation_whose_argument_is_a_call_records_null_args():
    """`compensate a.y(a.q(t))`: capturing the argument would call `a.q` at
    registration, on every successful call. The descriptor says the arguments
    were not captured instead of recording a list that looks complete."""
    from _backend_import import backend_emitter
    code = backend_emitter("python").emit(compile_source(
        "service A { emission fn x(t: Str) -> Int\n"
        "  emission fn y(t: Str) -> Int\n  fn q(t: Str) -> Str }\n"
        "service Ops { emission fn run(t: Str) -> Int }\n"
        "component C requires a: A provides ops: Ops {\n"
        "  provide ops { fn run(t) {\n"
        "    emit a.x(t) compensate a.y(a.q(t))\n"
        "    emit a.x(t) compensate a.y(t)\n    return 1 } }\n}\n", "q.rvl"))
    assert "call={'receiver': 'a', 'method': 'y', 'args': None}" in code
    assert "call={'receiver': 'a', 'method': 'y', 'args': [t]}" in code
