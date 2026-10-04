"""Three fresh-process runtime entry points `revl recover` needs (issue #1477,
the families PR #1494 left `unbound-residue`).

* `runtime.reissue_deferred`: owed deferred emissions a crashed session never
  flushed. They fire through the session's own flush (`SessionOwner._flush`),
  with the E-Stop check before each host body and a `flushed` or
  `flush-residue` record after it. A seq that already has either record is
  not fired again.
* `Stream.close(cursor)`: the re-issuable inverse a durable-cursor
  subscription's WAL record names, now a callable classmethod, so
  `replay_descriptors` resolves it instead of answering `unresolved`.
* `runtime.reclaim_shared`: a `shared` grant a crash left counted is fenced
  durably before its inverse runs, and `shared-complete` is written only after
  the inverse returns. A handle with either record already is not re-fired.

Each entry point runs in a fresh process with the emitted module loaded and not
activated. The deferred tests need the pinned cordis fork to write the WAL;
without it they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
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
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _records(wal: Path) -> list:
    return [json.loads(line) for line in wal.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ------------------------------------------------------ owed deferred emission

DEFERRED = """
extern emission deferred fn deliver(t: Str) = @py {
    import os
    if t == 'boom':
        raise RuntimeError('the outbox refused ' + t)
    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:
        f.write('deliver:' + t + chr(10))
}
service Ops { emission fn queue(t: Str) }
component Agent provides ops: Ops {
  provide ops { fn queue(t) { emit deliver(t) } }
}
"""

REISSUE = r"""
import json, sys, types
root, source, wal = sys.argv[1:4]
sys.path.insert(0, root + "/backends/python")
sys.path.insert(0, root + "/src")
sys.path.insert(0, root + "/tests")
import runtime
from revl.compiler import compile_source
from _backend_import import backend_emitter
module = types.ModuleType("reissued_composition")
exec(compile(backend_emitter("python").emit(compile_source(open(source).read(),
     "agent.rvl")), "agent_emitted", "exec"), module.__dict__)
records = [json.loads(line) for line in open(wal) if line.strip()]
owed = [r for r in records if r.get("record") == "deferred-emission"]
print(json.dumps({str(k): v for k, v in
                  runtime.reissue_deferred(module, wal, owed).items()}))
"""


@pytest.fixture
def crashed_queue(tmp_path, monkeypatch):
    """Queue three deferred emissions, then stop as a crash would: no commit,
    no flush."""
    log = tmp_path / "world.log"
    monkeypatch.setenv("REVL_R_LOG", str(log))
    (tmp_path / "agent.rvl").write_text(DEFERRED, encoding="utf-8")
    wal = tmp_path / "run.wal"
    from revl.mcp.session import Session
    session = Session()
    session._wal_path = str(wal)
    session.load(compile_source(DEFERRED, "agent.rvl"), record=True)
    session._ensure_wal_open()
    for t in ("a", "boom", "b"):
        session.call("ops", "queue", [t])
    yield {"dir": tmp_path, "log": log, "wal": wal}
    session._reset()


def _reissue(state, **env) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", REISSUE, str(ROOT), "agent.rvl", "run.wal"],
        cwd=str(state["dir"]), capture_output=True, text=True, timeout=600,
        env=dict(os.environ, **env))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return {int(k): v for k, v in json.loads(proc.stdout.splitlines()[-1]).items()}


def _lines(path: Path) -> list:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@needs_cordis
def test_owed_deferred_emissions_fire_in_program_order_and_are_recorded(crashed_queue):
    assert _lines(crashed_queue["log"]) == []          # nothing flushed yet
    outcome = _reissue(crashed_queue)
    owed = [r["seq"] for r in _records(crashed_queue["wal"])
            if r.get("record") == "deferred-emission"]
    assert [outcome[s] for s in owed] == ["ran", "failed", "ran"]
    assert _lines(crashed_queue["log"]) == ["deliver:a", "deliver:b"]
    records = _records(crashed_queue["wal"])
    assert {r["seq"] for r in records if r.get("record") == "flushed"} == {owed[0], owed[2]}
    [residue] = [r for r in records if r.get("record") == "flush-residue"]
    assert residue["seq"] == owed[1]
    assert "the outbox refused boom" in residue["error"]["message"]


@needs_cordis
def test_a_second_reissue_fires_nothing(crashed_queue):
    _reissue(crashed_queue)
    before = _lines(crashed_queue["log"])
    outcome = _reissue(crashed_queue)
    assert set(outcome.values()) == {"settled"}
    assert _lines(crashed_queue["log"]) == before


@needs_cordis
def test_an_armed_latch_strands_every_owed_emission(crashed_queue):
    latch = crashed_queue["dir"] / "estop.latch"
    latch.write_text(json.dumps({"reason": "drill", "operator": "ops"}),
                     encoding="utf-8")
    wal_before = crashed_queue["wal"].read_text(encoding="utf-8")
    outcome = _reissue(crashed_queue, REVL_ESTOP_LATCH=str(latch))
    assert set(outcome.values()) == {"stranded"}
    assert _lines(crashed_queue["log"]) == []
    assert crashed_queue["wal"].read_text(encoding="utf-8") == wal_before


# -------------------------------------------------- the durable-cursor inverse


def test_stream_close_names_a_callable_inverse_and_keeps_the_position():
    import runtime
    source = runtime.Stream.source(replay={"cursor": "orders"})
    sub = runtime.Stream.subscribe(source, replay={"cursor": "orders"})
    try:
        descriptor = sub.durable_undo().descriptor()["op"]
        assert descriptor == {"receiver": "Stream", "method": "close",
                              "args": ["orders"]}
        target = runtime._resolve_call(types.ModuleType("m"), {}, descriptor)
        assert target is not None
        position = runtime.Stream.cursor_at("orders")
        assert target(*descriptor["args"]) is True     # the live one closes
        assert sub._closed
        assert runtime.Stream.cursor_at("orders") == position
        assert target(*descriptor["args"]) is False    # idempotent
    finally:
        sub.close()
        source.close()


def test_a_durable_cursor_descriptor_replays_through_the_runtime(tmp_path):
    """In a fresh process the listener died with the process that held it, so
    the inverse has nothing left to close, and it RUNS rather than answering
    `unresolved`."""
    import runtime
    wal = tmp_path / "run.wal"
    descriptor = {"record": "discharge-descriptor", "seq": 0,
                  "entry": "transactional",
                  "call": {"receiver": "Stream", "method": "close",
                           "args": ["orders"]},
                  "undo_idempotent": True}
    outcome = runtime.replay_descriptors(types.ModuleType("m"), str(wal),
                                         [descriptor])
    assert outcome == {0: "ran"}


# ---------------------------------------------------------- the shared reclaim


def _lock_module(log: list, fail: bool = False) -> types.ModuleType:
    module = types.ModuleType("shared_composition")

    def release_lock(name):
        if fail:
            raise RuntimeError("the lock server is down")
        log.append(name)
    module.release_lock = release_lock
    return module


def _grant(handle: str, holders: list) -> dict:
    return {"record": "shared-grant", "handle": handle, "holders": holders,
            "inverse": {"receiver": None, "method": "release_lock",
                        "args": [f"lock-{handle}"]}}


def _write(wal: Path, records: list) -> None:
    wal.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_a_counted_grant_is_fenced_then_reclaimed_then_completed(tmp_path):
    import runtime
    wal = tmp_path / "run.wal"
    grants = [_grant("h1", ["a"]), _grant("h2", ["a", "b"]), _grant("h2", ["b"])]
    _write(wal, grants)
    log: list = []
    outcome = runtime.reclaim_shared(_lock_module(log), str(wal), grants)
    assert outcome == {"h1": "ran", "h2": "ran"}
    assert log == ["lock-h1", "lock-h2"]
    tail = [(r["record"], r.get("handle")) for r in _records(wal)
            if r["record"] in ("shared-reclaim-fence", "shared-complete")]
    assert tail == [("shared-reclaim-fence", "h1"), ("shared-complete", "h1"),
                    ("shared-reclaim-fence", "h2"), ("shared-complete", "h2")]
    # a second run finds both complete and re-fires nothing
    assert runtime.reclaim_shared(_lock_module(log), str(wal), grants) == {
        "h1": "settled", "h2": "settled"}
    assert log == ["lock-h1", "lock-h2"]


def test_a_fenced_grant_is_not_re_fired_and_a_balanced_one_is_settled(tmp_path):
    import runtime
    wal = tmp_path / "run.wal"
    grants = [_grant("h1", ["a"]), _grant("h2", [])]
    _write(wal, grants + [{"record": "shared-reclaim-fence", "handle": "h1"}])
    log: list = []
    assert runtime.reclaim_shared(_lock_module(log), str(wal), grants) == {
        "h1": "fenced", "h2": "settled"}
    assert log == []


def test_a_raising_reclaim_leaves_its_fence_and_no_completion(tmp_path):
    import runtime
    wal = tmp_path / "run.wal"
    grants = [_grant("h1", ["a"])]
    _write(wal, grants)
    assert runtime.reclaim_shared(_lock_module([], fail=True), str(wal),
                                  grants) == {"h1": "failed"}
    kinds = [r["record"] for r in _records(wal)]
    assert "shared-reclaim-fence" in kinds and "shared-complete" not in kinds
    # and a later run reads the fence as outcome unknown: not re-fired
    log: list = []
    assert runtime.reclaim_shared(_lock_module(log), str(wal), grants) == {
        "h1": "fenced"}
    assert log == []


def test_an_armed_latch_strands_the_reclaim_before_the_fence(tmp_path, monkeypatch):
    import runtime
    wal = tmp_path / "run.wal"
    grants = [_grant("h1", ["a"])]
    _write(wal, grants)
    latch = tmp_path / "estop.latch"
    latch.write_text(json.dumps({"reason": "drill", "operator": "ops"}),
                     encoding="utf-8")
    runtime.arm_estop_latch(str(latch))
    try:
        log: list = []
        assert runtime.reclaim_shared(_lock_module(log), str(wal), grants) == {
            "h1": "stranded"}
        assert log == []
        assert [r["record"] for r in _records(wal)] == ["shared-grant"]
    finally:
        runtime.arm_estop_latch(None)
        runtime.clear_estop()
