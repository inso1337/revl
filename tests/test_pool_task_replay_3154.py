"""A pool peer remembers the tasks it ran across a restart and refuses a task
whose ``issued_at`` is outside the charter's window (issue #3154).

Before, the record of tasks already run was a dict inside ``PeerRunner``, so
``revl pool serve`` forgot it on restart, and a task's signed ``issued_at`` was
never compared with the clock, so a task captured long ago was accepted as
fresh. A captured task could therefore run again, and get a second receipt, for
as long as the operator's signature verified.

Every test here fails on the tree before the change: the restarted runner runs
the task again, and a task issued years ago runs.
"""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from revl import peer_pool as pp  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl.lawful_retry import EffectClass  # noqa: E402

import test_pool_dispatch_524 as base  # noqa: E402

NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def task_at(record, when, *, task_id="t-1"):
    body = pd.build_task(
        pool_id=record["pool_id"],
        charter_digest=pp.canonical_digest(record), peer_id=base.WORKER,
        task_id=task_id, artifact=base.PURE_SOURCE,
        runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE,
        at=when,
        grant={"caps": sorted(base.ENTRY_CAPS), "budgets": {}},
        policy_digest=pp.canonical_digest(record))
    return pd.sign_task(body, base.OPERATOR_ID)


@pytest.fixture
def clock(monkeypatch):
    """The peer's clock, which a test moves."""
    state = {"now": NOW}
    monkeypatch.setattr(pd, "_utc_now", lambda: state["now"])
    return state


def runner_with_record(tmp_path, record, **kwargs):
    return pd.PeerRunner(
        charter_record=record, identity=base.WORKER_ID,
        operator_public=base.OPERATOR_ID.public(),
        workspace=tmp_path / "peer-work", timeout=120.0,
        seen_path=tmp_path / "peer-work" / pd.SEEN_FILE, **kwargs)


# ---------------------------------------------------------------------------
# freshness
# ---------------------------------------------------------------------------


def test_a_task_signed_long_ago_is_refused_and_never_run(tmp_path, clock):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = base.peer_runner(tmp_path, record)
    old = task_at(record, datetime(2020, 1, 1, tzinfo=timezone.utc))
    answer = runner.handle(old)
    assert answer["ok"] is False
    assert answer["link"] == pd.LINK_STALE_TASK, answer
    assert "2020-01-01" in answer["reason"]
    assert not (tmp_path / "peer-work" / "t-1").exists(), "the task ran"
    assert runner.seen == {}


def test_the_window_is_the_charters_in_both_directions(tmp_path, clock):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    window = record["join_window_s"]
    runner = base.peer_runner(tmp_path, record)
    just_in = NOW - timedelta(seconds=window)
    just_out = NOW - timedelta(seconds=window + 1)
    assert runner.handle(task_at(record, just_in, task_id="in"))["ok"]
    assert runner.handle(task_at(record, just_out, task_id="out"))["link"] \
        == pd.LINK_STALE_TASK
    # A task dated ahead of the peer's clock by more than the window is as
    # suspect as an old one: the join check reads the distance both ways.
    ahead = NOW + timedelta(seconds=window + 1)
    assert runner.handle(task_at(record, ahead, task_id="ahead"))["link"] \
        == pd.LINK_STALE_TASK


def test_a_wider_charter_window_admits_an_older_task(tmp_path, clock):
    record = base.make_charter(join_window_s=7200)
    base.pool_on_disk(tmp_path, charter=record)
    runner = base.peer_runner(tmp_path, record)
    hour_old = task_at(record, NOW - timedelta(hours=1))
    assert runner.handle(hour_old)["ok"]
    narrow = base.make_charter(join_window_s=60)
    other = base.peer_runner(tmp_path / "narrow", narrow)
    assert other.handle(task_at(narrow, NOW - timedelta(minutes=5)))["link"] \
        == pd.LINK_STALE_TASK


@pytest.mark.parametrize("window", [None, 0, -5, "300", True, 1.5])
def test_a_missing_or_mangled_window_does_not_switch_the_check_off(
        tmp_path, clock, window):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    held = dict(record)
    if window is None:
        held.pop("join_window_s")
    else:
        held["join_window_s"] = window
    assert pd._task_window(held) == pd.DEFAULT_TASK_WINDOW_S
    # The task pins the digest of the charter as handed over, so build it
    # against the mangled record the peer holds.
    runner = base.peer_runner(tmp_path, held)
    old = task_at(held, NOW - timedelta(days=30))
    assert runner.handle(old)["link"] == pd.LINK_STALE_TASK


def test_an_issued_at_that_is_not_a_time_is_refused(tmp_path, clock):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    body = dict(pd.build_task(
        pool_id=record["pool_id"],
        charter_digest=pp.canonical_digest(record), peer_id=base.WORKER,
        task_id="t-nt", artifact=base.PURE_SOURCE,
        runner=pd.RUNNER_TEST_PY, effect_class=EffectClass.PURE,
        grant={"caps": sorted(base.ENTRY_CAPS), "budgets": {}},
        policy_digest=pp.canonical_digest(record)))
    body["issued_at"] = "yesterday-ish"
    task = pd.sign_task(body, base.OPERATOR_ID)
    answer = base.peer_runner(tmp_path, record).handle(task)
    assert answer["link"] == pd.LINK_STALE_TASK, answer


def test_a_task_is_judged_by_when_it_arrived_not_when_the_lock_freed(
        tmp_path, clock):
    """The peer runs one task at a time. A fresh task queued behind a run that
    takes longer than the window is still a fresh task."""
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = base.peer_runner(tmp_path, record)
    task = task_at(record, NOW)
    answers = []
    read = threading.Event()

    def clock_read():
        read.set()
        return clock["now"]

    pd._utc_now = clock_read      # restored when the `clock` fixture ends
    runner._handle_lock.acquire()
    worker = threading.Thread(target=lambda: answers.append(runner.handle(task)))
    worker.start()
    assert read.wait(30), "the peer never read the clock"
    clock["now"] = NOW + timedelta(hours=2)
    runner._handle_lock.release()
    worker.join(timeout=120)
    assert answers and answers[0]["ok"], answers


# ---------------------------------------------------------------------------
# memory across a restart
# ---------------------------------------------------------------------------


def test_a_restarted_peer_refuses_a_task_it_already_ran(tmp_path, clock):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    task = task_at(record, NOW)
    first = runner_with_record(tmp_path, record).handle(task)
    assert first["ok"], first
    # A new object over the same workspace is what a restart is.
    restarted = runner_with_record(tmp_path, record)
    assert "t-1" in restarted.seen
    again = restarted.handle(task)
    assert again["ok"] is False
    assert again["link"] == pd.LINK_REPLAYED_TASK, again


def test_the_record_is_append_only_and_names_each_task_once(tmp_path, clock):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = runner_with_record(tmp_path, record)
    for task_id in ("a", "b"):
        assert runner.handle(task_at(record, NOW, task_id=task_id))["ok"]
    runner.handle(task_at(record, NOW, task_id="a"))    # refused, not written
    lines = (tmp_path / "peer-work" / pd.SEEN_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["task_id"] for line in lines] == ["a", "b"]


def test_a_task_id_is_written_down_before_the_task_runs(tmp_path, clock,
                                                         monkeypatch):
    """A peer that dies mid-run must not run the task again on restart."""
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = runner_with_record(tmp_path, record)

    def die(*_a, **_k):
        raise KeyboardInterrupt("the process went away mid-run")

    monkeypatch.setattr(pd, "execute", die)
    with pytest.raises(KeyboardInterrupt):
        runner.handle(task_at(record, NOW))
    monkeypatch.undo()
    restarted = runner_with_record(tmp_path, record)
    assert "t-1" in restarted.seen


def test_a_peer_that_cannot_record_a_task_runs_nothing(tmp_path, clock,
                                                         monkeypatch):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    runner = runner_with_record(tmp_path, record)
    # The disk goes bad after the peer started: a record path whose parent is
    # a file cannot be written.
    runner.seen_path = blocker / pd.SEEN_FILE
    ran = []
    monkeypatch.setattr(pd, "execute", lambda *a, **k: ran.append(a))
    answer = runner.handle(task_at(record, NOW))
    assert answer["ok"] is False
    assert answer["link"] == pd.LINK_PEER_STATE, answer
    assert ran == [] and runner.seen == {}


def test_a_peer_whose_record_cannot_be_read_does_not_start(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(pd.DispatchError, match="cannot read"):
        pd._load_seen(blocker / pd.SEEN_FILE)


def test_a_torn_last_line_is_cut_off_and_a_bad_line_stops_the_peer(tmp_path):
    path = tmp_path / pd.SEEN_FILE
    path.write_bytes(b'{"task_id": "a"}\n{"task_id": "b"}\n{"task_i')
    assert set(pd._load_seen(path)) == {"a", "b"}
    assert path.read_bytes() == b'{"task_id": "a"}\n{"task_id": "b"}\n'
    path.write_bytes(b'{"task_id": "a"}\nnot json\n{"task_id": "b"}\n')
    with pytest.raises(pd.DispatchError, match="line 2"):
        pd._load_seen(path)
    path.write_bytes(b'{"task_id": ""}\n')
    with pytest.raises(pd.DispatchError, match="line 1"):
        pd._load_seen(path)


def test_without_a_record_path_nothing_is_written(tmp_path, clock):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = base.peer_runner(tmp_path, record)
    assert runner.seen_path is None
    assert runner.handle(task_at(record, NOW))["ok"]
    assert not list((tmp_path / "peer-work").glob("*.jsonl"))
    assert "t-1" in runner.seen


def test_the_record_name_cannot_be_a_task_id():
    """A task's workspace is ``<workdir>/<task_id>``; no task id may name the
    record file that lives in the same directory."""
    record = base.make_charter()
    body = dict(task_at(record, NOW))
    body["task_id"] = pd.SEEN_FILE
    assert "task_id must be" in pd._task_shape(body)


def test_a_short_write_is_a_failure_to_record_and_runs_nothing(
        tmp_path, clock, monkeypatch):
    """A full disk can write less than asked without raising. A fragment is
    cut off at the next start, so the id would be forgotten and the task could
    run again."""
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = runner_with_record(tmp_path, record)
    real_write = pd.os.write
    monkeypatch.setattr(pd.os, "write",
                        lambda fd, data: real_write(fd, data[:5]))
    ran = []
    monkeypatch.setattr(pd, "execute", lambda *a, **k: ran.append(a))
    answer = runner.handle(task_at(record, NOW))
    assert answer["link"] == pd.LINK_PEER_STATE, answer
    assert "wrote 5 of" in answer["reason"]
    assert ran == [] and runner.seen == {}


# ---------------------------------------------------------------------------
# `revl pool serve`
# ---------------------------------------------------------------------------


class _Args:
    def __init__(self, tmp_path, workdir):
        from revl import peer_identity as pi
        self.charter = str(tmp_path / "charter.json")
        self.identity_key = str(tmp_path / "peer.key")
        self.operator_public = str(tmp_path / "operator.pub")
        pi.write_private_identity(self.identity_key, base.WORKER_ID)
        pi.write_public_identity(self.operator_public,
                                 base.OPERATOR_ID.public())
        self.workdir = str(workdir) if workdir else None
        self.timeout = 120.0
        self.host, self.port = "127.0.0.1", 0
        self.allow_remote, self.once = False, False


def _serve_once_over(monkeypatch, task):
    """Run `serve_command` with the socket loop replaced by one `handle`, and
    return that answer and the exit code."""
    answers = []

    def fake_serve(runner, **_kw):
        answers.append(runner.handle(task))

    monkeypatch.setattr(pd, "serve", fake_serve)
    return answers


def test_pool_serve_with_a_workdir_remembers_across_a_restart(
        tmp_path, clock, monkeypatch, capsys):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    (tmp_path / "charter.json").write_text(json.dumps(record), encoding="utf-8")
    workdir = tmp_path / "peer-work"
    args = _Args(tmp_path, workdir)
    task = task_at(record, NOW)
    answers = _serve_once_over(monkeypatch, task)
    assert pd.serve_command(args) == 0
    assert pd.serve_command(args) == 0       # the restart
    assert answers[0]["ok"], answers[0]
    assert answers[1]["link"] == pd.LINK_REPLAYED_TASK, answers[1]
    assert (workdir / pd.SEEN_FILE).is_file()
    assert "not remembered" not in capsys.readouterr().err


def test_pool_serve_without_a_workdir_says_it_forgets(
        tmp_path, clock, monkeypatch, capsys):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    (tmp_path / "charter.json").write_text(json.dumps(record), encoding="utf-8")
    _serve_once_over(monkeypatch, task_at(record, NOW))
    assert pd.serve_command(_Args(tmp_path, None)) == 0
    assert "not remembered across a restart" in capsys.readouterr().err


def test_pool_serve_exits_2_on_a_run_record_it_cannot_read(
        tmp_path, monkeypatch, capsys):
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    (tmp_path / "charter.json").write_text(json.dumps(record), encoding="utf-8")
    workdir = tmp_path / "peer-work"
    workdir.mkdir()
    (workdir / pd.SEEN_FILE).write_text("garbage\n", encoding="utf-8")
    served = _serve_once_over(monkeypatch, {})
    assert pd.serve_command(_Args(tmp_path, workdir)) == 2
    assert served == []
    assert "not a run record" in capsys.readouterr().err


def test_a_stale_task_that_also_ran_reports_stale(tmp_path, clock):
    """Freshness is judged first: it needs no state, and an old task is
    refused on its age whether or not it ran."""
    _d, record, _r, _dir = base.pool_on_disk(tmp_path)
    runner = runner_with_record(tmp_path, record)
    task = task_at(record, NOW)
    assert runner.handle(task)["ok"]
    clock["now"] = NOW + timedelta(hours=1)
    assert runner.handle(task)["link"] == pd.LINK_STALE_TASK
