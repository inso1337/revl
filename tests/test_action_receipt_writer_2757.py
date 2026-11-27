"""The runtime writer for signed action receipts (issue #2757, second slice).

`tests/test_action_receipt_chain_2757.py` pins the receipt, the chain and the
verifier. These tests pin the part that sits at the crossing:

  * a real write-ahead log, with the writer attached as its observer, yields a
    chain that VERIFIES, in the order the log recorded the crossings, for each
    of the three consequential records (an emission, a deferred emission that
    fired, an approval spend);
  * a declared `Secret[T]` argument reaches the receipt as a suppressed
    commitment with no digest, and the secret value appears nowhere in the
    chain, because the log redacted it before the receipt saw it;
  * what the log does NOT say is not guessed: an approval whose crossing never
    returned is recorded as unresolved, a deferral that never fired has no
    receipt and is counted, and a log that records no session verdict is
    refused until the caller names one;
  * the fail direction: a record the writer cannot read is a gap, the export
    refuses while a gap stands, and the observer never raises into the run;
  * the CLI reaches it: `revl receipts export --wal` signs a chain from a log an
    earlier run left, and `revl run --receipts-out` is refused without a log.
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

from revl import action_receipt as R  # noqa: E402
from revl import action_receipt_writer as W  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp.session import replay_module  # noqa: E402
from revl.wal import read_wal  # noqa: E402

KEY = b"an-action-receipt-writer-test-key"
SALT = "ab" * 32
NOW = "2026-01-01T00:00:00+00:00"
SECRET_VALUE = "hunter2-the-secret-token"
KEY_ENVS = (R.KEY_ENV, R.KEY_FILE_ENV)

replay = replay_module()

#: A composition declaring one `Secret[T]` parameter, in the IR shape the
#: recorder's `SecretIndex` reads: `charge(card: Secret, amount)`.
IR = {
    "services": {"pay": {"methods": {
        "charge": {"params": [{"name": "card", "secret": True},
                              {"name": "amount"}]},
        "audit": {"params": [{"name": "note"}]},
    }}},
    "externs": [],
    "components": [{"name": "shop", "requires": {"pay": "pay"}}],
}


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    for name in KEY_ENVS:
        monkeypatch.delenv(name, raising=False)


def _recorder(**overrides) -> W.ReceiptRecorder:
    options = {"worker": "worker-1", "task": "task-1"}
    options.update(overrides)
    return W.ReceiptRecorder(**options)


def _open_log(tmp_path, recorder=None, ir=IR):
    """A real write-ahead log, with `recorder` as its observer from the first
    record on, and a timeline bound to it for recording emissions."""
    wal = replay.WriteAheadLog(str(tmp_path / "run.wal"), ir)
    if recorder is not None:
        wal.attach_observer(recorder.observe)
    wal.open()
    timeline = replay.Timeline("shop", replay.Sources(),
                               replay.confidential.SecretIndex(ir))
    timeline.attach_wal(wal, ir)
    return wal, timeline


def _emit(timeline, method, *args):
    return timeline.record_emission("pay", method, args, "pay", (None, None))


# ---------------------------------------------------------------------------
# a real log, observed live
# ---------------------------------------------------------------------------

def test_a_live_log_yields_a_chain_that_verifies(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "audit", "first")
    _emit(timeline, "audit", "second")
    wal.record_commit_approved("f" * 64)
    chain = recorder.export(KEY, session="s1", salt=SALT, now=NOW)
    assert chain["count"] == 2
    assert chain["session_verdict"] == "committed"
    verdict = R.verify_chain(chain, KEY)
    assert verdict.ok, verdict.reason
    first, second = chain["receipts"]
    assert first["receipt_kind"] == W.EFFECT_CROSSING
    assert first["component"] == "shop"
    assert first["capability"] == "pay.audit"
    assert first["worker"] == "worker-1" and first["task"] == "task-1"
    assert first["input"]["mode"] == "salted"
    # the order of the chain is the order the log recorded the crossings
    assert second["prev"] == first["digest"]


def test_the_chain_binds_the_bundle_digest_the_log_header_names(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "audit", "x")
    wal.record_commit_approved("f" * 64)
    header = read_wal(wal.path)["header"]
    assert header["record"] == "header"
    bundle = header["composition"].removeprefix("sha256:")
    chain = recorder.export(KEY, session="s1")
    assert chain["receipts"][0]["bundle"] == bundle


def test_a_declared_secret_argument_is_suppressed_and_never_in_the_chain(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "charge", SECRET_VALUE, 10)
    wal.record_commit_approved("f" * 64)
    chain = recorder.export(KEY, session="s1")
    receipt = chain["receipts"][0]
    assert receipt["input"] == {"mode": "suppressed", "value": None,
                                "reason": "secret-origin"}
    assert receipt["input_origins"] == ["secret"]
    # the result is not secret, so it is committed to as a digest
    assert receipt["result"]["mode"] == "salted"
    assert R.verify_chain(chain, KEY).ok
    assert SECRET_VALUE not in json.dumps(chain)
    assert SECRET_VALUE not in Path(wal.path).read_text(encoding="utf-8")


def test_an_emission_with_no_outcome_record_says_unsettled(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "audit", "x")
    wal.record_commit_approved("f" * 64)
    [action] = recorder.actions()
    assert action["result"] == {"outcome": W.OUTCOME_UNSETTLED}


def test_an_emission_outcome_record_settles_the_receipt(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    step = _emit(timeline, "audit", "x")
    wal.record_emission_outcome(step.wal_seq, "emission-failed",
                                detail={"type": "Boom"})
    [action] = recorder.actions()
    assert action["result"]["outcome"] == "emission-failed"
    assert action["result"]["error"] == {"type": "Boom"}


def test_a_deferred_emission_that_fired_has_a_receipt_at_its_flush(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    queued = wal.record_deferred_emission(receiver="pay", method="audit",
                                          args=["note"])
    _emit(timeline, "audit", "immediate")
    wal.record_commit_approved("f" * 64)
    wal.record_flushed(queued["seq"])
    actions = recorder.actions()
    # the immediate crossing fired first, so it comes first in the chain
    assert [a["input"]["args"] for a in actions] == [["immediate"], ["note"]]
    assert actions[1]["result"] == {"outcome": "flushed"}
    assert R.verify_chain(recorder.export(KEY, session="s1"), KEY).ok


def test_a_deferred_emission_that_never_fired_has_no_receipt_and_is_counted(tmp_path):
    recorder = _recorder()
    wal, _ = _open_log(tmp_path, recorder)
    wal.record_deferred_emission(receiver="pay", method="audit", args=["x"])
    wal.record_aborted([])
    assert recorder.actions() == []
    assert recorder.unsettled == 1
    chain = recorder.export(KEY, session="s1")
    assert chain["count"] == 0 and chain["session_verdict"] == "aborted"


def test_an_approval_spend_joins_the_emission_it_authorised(tmp_path):
    recorder = _recorder()
    wal, _ = _open_log(tmp_path, recorder)
    wal.record_approval_consumed("req-1", use=1)
    wal.record_approval_emission("req-1", "pay.charge", "shop", use=1)
    wal.record_commit_approved("f" * 64)
    [action] = recorder.actions()
    assert action["receipt_kind"] == W.APPROVAL_USE
    assert action["approval"] == {"request_id": "req-1", "use": "1"}
    assert action["component"] == "shop" and action["capability"] == "pay.charge"
    assert action["result"] == {"fired": True}
    assert R.verify_chain(recorder.export(KEY, session="s1"), KEY).ok


def test_an_approval_whose_crossing_never_returned_is_recorded_not_dropped(tmp_path):
    recorder = _recorder()
    wal, _ = _open_log(tmp_path, recorder)
    wal.record_approval_consumed("req-2", use=1)
    wal.record_commit_approved("f" * 64)
    [action] = recorder.actions()
    assert action["component"] == W.UNRESOLVED
    assert action["capability"] == W.UNRESOLVED
    assert action["result"] == {"fired": False}
    assert R.verify_chain(recorder.export(KEY, session="s1"), KEY).ok


def test_a_log_with_no_observer_is_unchanged(tmp_path):
    """Opt-in: a log nobody observes writes the same records it always did."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    plain, plain_timeline = _open_log(plain_dir)
    _emit(plain_timeline, "audit", "x")
    plain.close()
    observed_dir = tmp_path / "observed"
    observed_dir.mkdir()
    wal, timeline = _open_log(observed_dir, _recorder())
    _emit(timeline, "audit", "x")
    wal.close()
    assert (read_wal(plain.path)["records"] == read_wal(wal.path)["records"])


# ---------------------------------------------------------------------------
# what the log does not say is not guessed
# ---------------------------------------------------------------------------

def test_a_log_that_records_no_verdict_is_refused_until_one_is_named(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "audit", "x")
    assert recorder.session_verdict() is None
    with pytest.raises(RevlError, match="verdict"):
        recorder.export(KEY, session="s1")
    chain = recorder.export(KEY, session="s1", session_verdict="aborted")
    assert chain["session_verdict"] == "aborted"


def test_a_log_with_contradictory_verdicts_is_not_read_as_either():
    recorder = _recorder()
    recorder.observe({"record": "commit-approved", "hash": "f" * 64})
    recorder.observe({"record": "aborted", "replayed": []})
    assert recorder.session_verdict() is None


def test_a_chain_spanning_a_reload_binds_each_crossing_to_its_own_bundle():
    first, second = "1" * 64, "2" * 64
    recorder = _recorder()
    recorder.observe({"record": "header", "composition": "sha256:" + first})
    recorder.observe({"record": "effect", "seq": 1, "component": "c",
                      "label": "a.b", "boundary": {"class": "emission",
                                                   "detail": {"args": []}}})
    recorder.observe({"record": "generation", "composition": "sha256:" + second})
    recorder.observe({"record": "effect", "seq": 2, "component": "c",
                      "label": "a.b", "boundary": {"class": "emission",
                                                   "detail": {"args": []}}})
    assert [a["bundle"] for a in recorder.actions()] == [first, second]


def test_non_emission_effects_are_not_consequential_actions():
    recorder = _recorder()
    recorder.observe({"record": "effect", "seq": 1, "component": "c",
                      "label": "x", "boundary": {"class": "in-process"}})
    recorder.observe({"record": "effect", "seq": 2, "component": "c",
                      "label": "x", "boundary": {"class": "compensation"}})
    assert recorder.actions() == []


# ---------------------------------------------------------------------------
# fail direction
# ---------------------------------------------------------------------------

def test_an_unreadable_record_is_a_gap_and_the_export_refuses():
    recorder = _recorder()
    recorder.observe("not a record")
    recorder.observe({"record": "effect", "seq": 1, "component": "c",
                      "label": "", "boundary": {"class": "emission"}})
    assert len(recorder.gaps) == 2
    with pytest.raises(RevlError, match="incomplete"):
        recorder.export(KEY, session="s1", session_verdict="committed")


def test_an_emission_for_an_unknown_spend_is_a_gap():
    recorder = _recorder()
    recorder.observe({"record": "approval-emission", "requestId": "ghost",
                      "capability": "c", "component": "k"})
    assert recorder.gaps and "ghost" in recorder.gaps[0]


def test_observe_never_raises_on_a_hostile_record():
    recorder = _recorder()
    hostile = [None, 7, [], {"record": ["unhashable"]},
               {"record": "effect", "boundary": {"class": "emission",
                                                 "detail": 5},
                "component": "c", "label": "l", "seq": {"x": 1}},
               {"record": "deferred-emission", "call": 3, "origin": []}]
    for record in hostile:
        recorder.observe(record)
    assert isinstance(recorder.gaps, list)


def test_a_placeholder_anywhere_in_the_arguments_suppresses_the_input():
    recorder = _recorder()
    nested = {"a": [1, {"b": "<redacted:secret>"}]}
    recorder.observe({"record": "effect", "seq": 1, "component": "c",
                      "label": "a.b", "boundary": {
                          "class": "emission", "detail": {"args": [nested]}}})
    [action] = recorder.actions()
    assert action["input_origins"] == ["secret"]


def test_a_value_too_deep_to_read_is_treated_as_secret_not_hashed():
    deep: object = "x"
    for _ in range(W._MAX_DEPTH + 5):
        deep = [deep]
    assert W._origins_of(deep) == ["secret"]


def test_the_recorder_refuses_missing_identity():
    with pytest.raises(ValueError):
        W.ReceiptRecorder(worker="", task="t")
    with pytest.raises(ValueError):
        W.ReceiptRecorder(worker="w", task="")
    with pytest.raises(ValueError):
        W.ReceiptRecorder(worker="w", task="t", policy="not-a-digest")


# ---------------------------------------------------------------------------
# the offline path and the CLI
# ---------------------------------------------------------------------------

def _write_log(tmp_path):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "charge", SECRET_VALUE, 10)
    _emit(timeline, "audit", "x")
    wal.record_commit_approved("f" * 64)
    wal.close()
    return Path(wal.path), recorder


def test_a_finished_log_reads_to_the_same_actions_as_the_live_writer(tmp_path):
    path, live = _write_log(tmp_path)
    offline = W.recorder_from_wal(read_wal(str(path)),
                                  worker="worker-1", task="task-1")
    assert offline.actions() == live.actions()
    # the header is what names the bundle, so the offline reader must see it
    assert offline.actions()[0]["bundle"] is not None


def test_cli_exports_a_chain_from_a_wal(tmp_path, capsys):
    path, _ = _write_log(tmp_path)
    key = tmp_path / "key"
    key.write_bytes(KEY)
    out = tmp_path / "chain.json"
    assert main(["receipts", "export", "--wal", str(path), "--worker", "w",
                 "--session", "s1", "--key", str(key), "--out", str(out)]) == 0
    chain = json.loads(out.read_text(encoding="utf-8"))
    assert chain["count"] == 2 and chain["session_verdict"] == "committed"
    assert chain["receipts"][0]["task"] == "s1"
    assert SECRET_VALUE not in out.read_text(encoding="utf-8")
    assert main(["receipts", "verify", str(out), "--key", str(key)]) == 0


def test_cli_wal_export_needs_a_worker(tmp_path, capsys):
    path, _ = _write_log(tmp_path)
    key = tmp_path / "key"
    key.write_bytes(KEY)
    assert main(["receipts", "export", "--wal", str(path), "--session", "s1",
                 "--key", str(key)]) == 1
    assert "--worker" in capsys.readouterr().err


def test_cli_wal_export_names_the_verdict_it_cannot_derive(tmp_path, capsys):
    recorder = _recorder()
    wal, timeline = _open_log(tmp_path, recorder)
    _emit(timeline, "audit", "x")
    wal.close()
    key = tmp_path / "key"
    key.write_bytes(KEY)
    base = ["receipts", "export", "--wal", str(wal.path), "--worker", "w",
            "--session", "s1", "--key", str(key)]
    assert main(base) == 1
    assert "verdict" in capsys.readouterr().err
    assert main(base + ["--session-verdict", "aborted"]) == 0


def test_cli_run_refuses_receipts_without_a_wal(tmp_path, capsys):
    key = tmp_path / "key"
    key.write_bytes(KEY)
    source = tmp_path / "c.rvl"
    source.write_text("component c { }\n", encoding="utf-8")
    assert main(["run", str(source), "--receipts-out", str(tmp_path / "r.json"),
                 "--receipts-worker", "w", "--receipts-key", str(key)]) == 2
    assert "--wal" in capsys.readouterr().err


def test_cli_run_refuses_receipts_without_a_worker_or_a_key(tmp_path, capsys):
    source = tmp_path / "c.rvl"
    source.write_text("component c { }\n", encoding="utf-8")
    wal = str(tmp_path / "x.wal")
    assert main(["run", str(source), "--wal", wal,
                 "--receipts-out", str(tmp_path / "r.json")]) == 2
    assert "--receipts-worker" in capsys.readouterr().err
    assert main(["run", str(source), "--wal", wal,
                 "--receipts-out", str(tmp_path / "r.json"),
                 "--receipts-worker", "w"]) == 2
    assert "no receipt key" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# a live run, end to end (needs the pinned cordis fork; SKIPS without it)
# ---------------------------------------------------------------------------

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="`revl run` boots a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")

LIVE = """
extern emission fn note(t: Str) -> Unit = @py {
    return None
}
component Agent {
  emit note("first")
  emit note("second")
}
"""


def _revl(*argv, cwd):
    return subprocess.run(
        [sys.executable, "-m", "revl", *argv], cwd=str(cwd),
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        capture_output=True, text=True, timeout=600)


@needs_cordis
def test_revl_run_writes_a_verifying_chain_at_teardown(tmp_path):
    (tmp_path / "agent.rvl").write_text(LIVE, encoding="utf-8")
    key = tmp_path / "key"
    key.write_bytes(KEY)
    proc = _revl("run", "agent.rvl", "--once", "--wal", "run.wal",
                 "--receipts-out", "chain.json", "--receipts-worker", "w-1",
                 "--receipts-key", str(key), cwd=tmp_path)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    chain = json.loads((tmp_path / "chain.json").read_text(encoding="utf-8"))
    assert chain["session"] == "run" and chain["session_verdict"] == "committed"
    assert [r["capability"] for r in chain["receipts"]] == ["note", "note"]
    assert chain["receipts"][0]["worker"] == "w-1"
    assert R.verify_chain(chain, KEY).ok
    # the same log, read offline, yields the same crossings
    check = _revl("receipts", "export", "--wal", "run.wal", "--worker", "w-1",
                  "--session", "run", "--session-verdict", "committed",
                  "--key", str(key), cwd=tmp_path)
    assert check.returncode == 0, check.stderr[-2000:]
    offline = json.loads(check.stdout)
    assert [r["capability"] for r in offline["receipts"]] == ["note", "note"]


# ---------------------------------------------------------------------------
# review findings, pinned
# ---------------------------------------------------------------------------

def _emission(seq=1, label="a.b", args=None, **extra):
    return {"record": "effect", "seq": seq, "component": "c", "label": label,
            "kind": "emission",
            "boundary": {"class": "emission", "detail": {"args": args or []}},
            **extra}


def test_a_seq_journalled_twice_before_it_settles_is_a_gap():
    recorder = _recorder()
    recorder.observe(_emission(seq=0))
    recorder.observe(_emission(seq=0))
    assert recorder.gaps and "twice" in recorder.gaps[0]
    with pytest.raises(RevlError, match="incomplete"):
        recorder.export(KEY, session="s", session_verdict="committed")


def test_a_deferred_seq_journalled_twice_is_a_gap():
    recorder = _recorder()
    call = {"receiver": "pay", "method": "audit", "args": []}
    recorder.observe({"record": "deferred-emission", "seq": 3, "call": call})
    recorder.observe({"record": "deferred-emission", "seq": 3, "call": call})
    assert recorder.gaps and "deferred" in recorder.gaps[0]


def test_a_seq_may_be_reused_once_it_has_settled():
    recorder = _recorder()
    recorder.observe(_emission(seq=0))
    recorder.observe({"record": "emission-complete", "seq": 0})
    recorder.observe(_emission(seq=0))
    assert recorder.gaps == []
    assert len(recorder.actions()) == 2


def test_an_emission_record_with_no_emission_boundary_is_a_gap():
    recorder = _recorder()
    recorder.observe({"record": "effect", "seq": 1, "component": "c",
                      "label": "a.b", "kind": "emission", "boundary": {}})
    assert recorder.gaps and "no emission boundary" in recorder.gaps[0]


def test_the_placeholder_is_found_in_text_and_in_keys_not_only_whole_values():
    for args in ([{"k": "prefix <redacted:secret> suffix"}],
                 [{"<redacted:secret>": 1}]):
        recorder = _recorder()
        recorder.observe(_emission(args=args))
        [action] = recorder.actions()
        assert action["input_origins"] == ["secret"], args


def test_a_secret_in_the_error_text_suppresses_the_result():
    recorder = _recorder()
    recorder.observe(_emission(seq=4))
    recorder.observe({"record": "emission-failed", "seq": 4,
                      "error": {"message": "refused <redacted:secret>"}})
    [action] = recorder.actions()
    assert action["result_origins"] == ["secret"]
    chain = recorder.export(KEY, session="s", session_verdict="committed")
    assert chain["receipts"][0]["result"]["mode"] == "suppressed"
    assert R.verify_chain(chain, KEY).ok


def test_a_log_with_both_verdict_markers_is_refused_even_with_a_named_verdict():
    recorder = _recorder()
    recorder.observe({"record": "commit-approved", "hash": "f" * 64})
    recorder.observe({"record": "aborted", "replayed": []})
    with pytest.raises(RevlError, match="contradicts itself"):
        recorder.export(KEY, session="s", session_verdict="committed")


def test_a_later_mutation_of_the_record_cannot_reach_the_receipt():
    recorder = _recorder()
    record = _emission(args=["original"])
    recorder.observe(record)
    record["boundary"]["detail"]["args"][0] = "mutated"
    assert recorder.actions()[0]["input"]["args"] == ["original"]


def test_cli_reports_a_deferral_that_never_settled(tmp_path, capsys):
    recorder = _recorder()
    wal, _ = _open_log(tmp_path, recorder)
    wal.record_deferred_emission(receiver="pay", method="audit", args=["x"])
    wal.close()
    key = tmp_path / "key"
    key.write_bytes(KEY)
    assert main(["receipts", "export", "--wal", str(wal.path), "--worker", "w",
                 "--session", "s1", "--session-verdict", "aborted",
                 "--key", str(key)]) == 0
    assert "1 deferred emission(s) never settled" in capsys.readouterr().err


BROKEN = """
extern emission fn note(t: Str) -> Unit = @py {
    return None
}
extern emission fn boom() -> Unit = @py {
    raise RuntimeError("the host failed")
}
component Agent {
  emit note("before")
  emit boom()
}
"""


@needs_cordis
def test_revl_run_receipts_a_failed_crossing_as_failed_and_replaces_a_stale_chain(tmp_path):
    (tmp_path / "agent.rvl").write_text(BROKEN, encoding="utf-8")
    key = tmp_path / "key"
    key.write_bytes(KEY)
    stale = tmp_path / "chain.json"
    stale.write_text('{"stale": true}', encoding="utf-8")
    proc = _revl("run", "agent.rvl", "--once", "--wal", "run.wal",
                 "--receipts-out", "chain.json", "--receipts-worker", "w-1",
                 "--receipts-key", str(key), cwd=tmp_path)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    # the stale file was replaced by this run's own chain, and nothing partial
    chain = json.loads(stale.read_text(encoding="utf-8"))
    assert chain["kind"] == R.CHAIN_KIND and chain["count"] == 2
    assert R.verify_chain(chain, KEY).ok
    assert not (tmp_path / "chain.json.partial").exists()
    # the crossing that failed says so: the second receipt's outcome is the log's
    offline = W.recorder_from_wal(read_wal(str(tmp_path / "run.wal")),
                                  worker="w-1", task="run")
    outcomes = [a["result"]["outcome"] for a in offline.actions()]
    assert outcomes == ["emission-complete", "emission-failed"]


def test_a_stale_chain_is_removed_when_the_run_is_refused_after_validation(tmp_path):
    """A chain already at the path is not this run's: it is removed once the
    flags validate, so a boot that fails cannot leave it verifying there."""
    key = tmp_path / "key"
    key.write_bytes(KEY)
    stale = tmp_path / "chain.json"
    stale.write_text('{"stale": true}', encoding="utf-8")
    broken = tmp_path / "broken.rvl"
    broken.write_text("this is not a composition", encoding="utf-8")
    main(["run", str(broken), "--wal", str(tmp_path / "x.wal"),
          "--receipts-out", str(stale), "--receipts-worker", "w",
          "--receipts-key", str(key)])
    assert not stale.exists()
