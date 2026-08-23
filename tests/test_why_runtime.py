"""Runtime "why" — causal lifecycle traces, and the prediction-vs-actuality
oracle (roadmap item 27, docs/why-runtime.md).

Two layers, tested to two honesty rules that mirror test_run.py:

* the pure layer (the trace vocabulary, the cause-chain walk, and the oracle
  diff) needs no runtime — it operates on JSONL events plus a compiled IR, so
  it runs on every interpreter, including the constructed-disagreement case
  the oracle must *flag*;
* the end-to-end layer — `revl run --withdraw --trace` actually booting a
  composition, disposing a provider, and recording the cascade the runtime
  really produced — runs only where cordis is present (`@needs_cordis`); a
  runtime-less interpreter skips with a reason rather than feigning a pass.

The oracle's whole claim is that a disagreement is never noise (a differential
oracle: neither side is allowed to be wrong on its own terms), so the flagging
tests are as load-bearing as the conformance ones.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import why_runtime as wr  # noqa: E402

USER_CACHE = str(ROOT / "examples" / "user_cache.rvl")


try:  # the same availability gate test_run.py / test_replay.py use
    import cordis  # noqa: F401
    HAVE_CORDIS = True
except ModuleNotFoundError:  # pragma: no cover — depends on the interpreter
    HAVE_CORDIS = False

needs_cordis = pytest.mark.skipif(
    not HAVE_CORDIS,
    reason="needs the cordis-py runtime (run under "
           "backends/python/.venv/bin/python)")


# ---- fixtures: hand-built traces (no runtime needed) --------------------

def _conforming_trace() -> list[dict]:
    """The trace `revl run --withdraw PgDatabase` produces for user_cache:
    PgDatabase withdrawn (trigger), UserCache deactivates because it injects
    `db`. Built by hand so the pure oracle test needs no runtime."""
    return [
        wr.make_event(0, 1, wr.LOAD, "PgDatabase", "PENDING -> ACTIVE",
                      wr.cause_boot()),
        wr.make_event(1, 1, wr.LOAD, "UserCache", "PENDING -> ACTIVE",
                      wr.cause_requirements([{"component": "PgDatabase", "key": "db"}])),
        wr.make_event(2, 1, wr.WITHDRAW, "UserCache", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("PgDatabase", "db")),
        wr.make_event(3, 1, wr.WITHDRAW, "PgDatabase", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("withdrawn by operator")),
    ]


# ---- the trace vocabulary + serialisation -------------------------------

def test_trace_round_trips_through_jsonl(tmp_path):
    events = _conforming_trace()
    path = tmp_path / "run.jsonl"
    wr.write_trace(events, str(path))
    # JSONL: one object per line, no blank lines swallowed by the reader
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == len(events)
    assert wr.read_trace(str(path)) == events


def test_parse_trace_rejects_non_events():
    with pytest.raises(ValueError):
        wr.parse_trace('{"not": "an event"}')
    with pytest.raises(ValueError):
        wr.parse_trace("this is not json")
    # blank lines are tolerated
    assert wr.parse_trace("\n\n") == []


# ---- the causal chain walk ----------------------------------------------

def test_cause_chain_follows_a_withdrawal_to_its_root():
    trace = wr.Trace(_conforming_trace())
    frames = trace.cause_chain("UserCache")
    assert [f.component for f in frames] == ["UserCache", "PgDatabase"]
    # the cascade edge, then the trigger root
    assert frames[0].cause["kind"] == wr.PROVIDER_WITHDRAWN
    assert frames[-1].cause["kind"] == wr.TRIGGER
    rendered = wr.render_chain("UserCache", frames)
    assert "why UserCache was withdrawn" in rendered
    assert "root cause" in rendered
    assert "PgDatabase" in rendered


def test_cause_chain_follows_a_load_to_boot():
    trace = wr.Trace(_conforming_trace())
    frames = trace.cause_chain("UserCache", prefer=wr.LOAD)
    assert [f.component for f in frames] == ["UserCache", "PgDatabase"]
    assert frames[0].event == wr.LOAD
    assert frames[-1].cause["kind"] == wr.BOOT


def test_cause_chain_reports_an_unrecorded_component():
    frames = wr.Trace(_conforming_trace()).cause_chain("Nonexistent")
    assert frames[0].cause["kind"] == "unrecorded"


# ---- the static prediction, in the trace vocabulary ---------------------

def test_predicted_cascade_reads_the_withdraw_query():
    ir = compile_files([USER_CACHE])
    predicted = wr.predicted_cascade(ir, "PgDatabase")
    assert predicted["ok"]
    assert predicted["broken"] == ["UserCache"]
    # LIFO: the dependent tears down first, the withdrawn component last
    assert predicted["order"] == ["UserCache", "PgDatabase"]


def test_predicted_cascade_reports_an_unknown_component():
    ir = compile_files([USER_CACHE])
    predicted = wr.predicted_cascade(ir, "Nope")
    assert not predicted["ok"]


# ---- the oracle: agreement ----------------------------------------------

def test_oracle_conforms_on_a_correct_cascade():
    ir = compile_files([USER_CACHE])
    report = wr.oracle(ir, "PgDatabase", wr.Trace(_conforming_trace()))
    assert report["ok"] and report["conforms"] is True
    assert not report["defects"]
    assert "CONFORMS" in wr.render_oracle(report)


def test_oracle_is_silent_when_no_withdrawal_is_recorded():
    ir = compile_files([USER_CACHE])
    # a load-only trace: nothing to check the prediction against
    loads = [e for e in _conforming_trace() if e["event"] == wr.LOAD]
    report = wr.oracle(ir, "PgDatabase", wr.Trace(loads))
    assert report["conforms"] is None
    assert "no withdrawal" in report["note"]


# ---- the oracle: flagging a disagreement (the point) --------------------

def test_oracle_flags_a_missing_teardown():
    """The runtime withdrew PgDatabase but UserCache never went down — the
    compiler proved it must. A real defect, not swallowed."""
    ir = compile_files([USER_CACHE])
    broken_trace = [
        e for e in _conforming_trace()
        if not (e["event"] == wr.WITHDRAW and e["component"] == "UserCache")
    ]
    report = wr.oracle(ir, "PgDatabase", wr.Trace(broken_trace))
    assert report["conforms"] is False
    kinds = {d["kind"] for d in report["defects"]}
    assert "missing-teardown" in kinds
    assert "DEFECT" in wr.render_oracle(report)


def test_oracle_flags_an_unexpected_teardown():
    """A component the prediction proves *survives* was torn down anyway."""
    ir = compile_files([USER_CACHE])
    trace = _conforming_trace() + [
        wr.make_event(4, 1, wr.WITHDRAW, "Bystander", "ACTIVE -> DISPOSED",
                      wr.cause_provider_withdrawn("PgDatabase", "db")),
    ]
    report = wr.oracle(ir, "PgDatabase", wr.Trace(trace))
    assert report["conforms"] is False
    assert any(d["kind"] == "unexpected-teardown" for d in report["defects"])


def test_oracle_flags_an_order_mismatch(tmp_path):
    """Same set of teardowns, wrong LIFO order — a three-deep chain lets the
    two dependents be swapped."""
    src = tmp_path / "chain.rvl"
    src.write_text(
        "service A { fn a() -> Int }\n"
        "service B { fn b() -> Int }\n"
        "service C { fn c() -> Int }\n"
        "component CompA provides a: A { provide a { fn a() = 1 } }\n"
        "component CompB requires a: A provides b: B { provide b { fn b() = 2 } }\n"
        "component CompC requires b: B provides c: C { provide c { fn c() = 3 } }\n",
        encoding="utf-8")
    ir = compile_files([str(src)])
    predicted = wr.predicted_cascade(ir, "CompA")
    # predicted LIFO order is CompC -> CompB -> CompA
    assert predicted["order"] == ["CompC", "CompB", "CompA"]

    # actual trace settles CompB before CompC — the wrong order, same set
    trace = [
        wr.make_event(0, 1, wr.WITHDRAW, "CompA", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("operator")),
        wr.make_event(1, 1, wr.WITHDRAW, "CompB", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("CompA", "a")),
        wr.make_event(2, 1, wr.WITHDRAW, "CompC", "ACTIVE -> PENDING",
                      wr.cause_provider_withdrawn("CompB", "b")),
    ]
    report = wr.oracle(ir, "CompA", wr.Trace(trace))
    assert report["conforms"] is False
    assert [d["kind"] for d in report["defects"]] == ["order-mismatch"]


# ---- the `revl why` CLI (no runtime needed to read a trace) -------------

def _cli(args, input_text=""):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "revl", *args],
                          capture_output=True, text=True, input=input_text,
                          env=env, check=False)


def test_why_cli_prints_the_cause_chain(tmp_path):
    path = tmp_path / "run.jsonl"
    wr.write_trace(_conforming_trace(), str(path))
    result = _cli(["why", "UserCache", "--trace", str(path)])
    assert result.returncode == 0, result.stderr
    assert "why UserCache was withdrawn" in result.stdout
    assert "provided by PgDatabase" in result.stdout


def test_why_cli_json_and_oracle_check(tmp_path):
    path = tmp_path / "run.jsonl"
    wr.write_trace(_conforming_trace(), str(path))
    result = _cli(["why", "PgDatabase", "--trace", str(path),
                   "--check", USER_CACHE, "--json"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["oracle"]["conforms"] is True
    assert payload["chain"][0]["component"] == "PgDatabase"


def test_why_cli_check_exits_nonzero_on_a_defect(tmp_path):
    # a trace with no cascade at all: the oracle must flag missing-teardown
    path = tmp_path / "bad.jsonl"
    wr.write_trace([
        wr.make_event(0, 1, wr.WITHDRAW, "PgDatabase", "ACTIVE -> DISPOSED",
                      wr.cause_trigger("operator")),
    ], str(path))
    result = _cli(["why", "PgDatabase", "--trace", str(path), "--check", USER_CACHE])
    assert result.returncode == 1
    assert "DEFECT" in result.stdout
    assert "missing-teardown" in result.stdout


# ---- end to end: the runtime actually produces a conforming trace -------

@needs_cordis
def test_run_withdraw_records_a_conforming_causal_trace(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[PgDatabase]\nurl = \"postgres://e2e:5432/app\"\n",
                   encoding="utf-8")
    trace_path = tmp_path / "run.jsonl"
    result = _cli(["run", USER_CACHE, "--backend", "py",
                   "--config", str(cfg), "--withdraw", "PgDatabase",
                   "--trace", str(trace_path)])
    assert result.returncode == 0, result.stderr + result.stdout
    # the run streams the cascade and its own oracle verdict
    assert "== withdraw PgDatabase" in result.stdout
    assert "CONFORMS" in result.stdout
    assert "no residue" in result.stdout

    # the recorded trace is a genuine causal record of what happened
    events = wr.read_trace(str(trace_path))
    withdraws = [e for e in events if e["event"] == wr.WITHDRAW]
    names = {e["component"] for e in withdraws}
    assert names == {"PgDatabase", "UserCache"}
    usercache = next(e for e in withdraws if e["component"] == "UserCache")
    assert usercache["cause"]["kind"] == wr.PROVIDER_WITHDRAWN
    assert usercache["cause"]["component"] == "PgDatabase"

    # and the oracle, run over the RECORDED (observed) trace against the
    # static prediction, agrees — the runtime did what the compiler computed
    ir = compile_files([USER_CACHE])
    report = wr.oracle(ir, "PgDatabase", wr.Trace(events))
    assert report["conforms"] is True


@needs_cordis
def test_run_withdraw_rejects_an_unknown_component(tmp_path):
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("[PgDatabase]\nurl = \"postgres://e2e:5432/app\"\n",
                   encoding="utf-8")
    result = _cli(["run", USER_CACHE, "--backend", "py",
                   "--config", str(cfg), "--withdraw", "Nonexistent"])
    # the run still tears down cleanly; the bad name is reported, not crashed
    assert "no live component named" in result.stdout
    assert result.returncode == 0
