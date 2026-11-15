"""Issue #2713: `revl simulate swap`, a recorded history re-run against a
changed component.

Every test here attacks a way the re-run could be faked.

* The preflight must NAME the missing fact and run nothing.
  `test_a_history_without_a_recorded_host_response_is_refused_with_the_fact_named`
  writes a crossing whose `begin` is on the record and whose `complete` is not,
  and requires the report to name that exact crossing, keep `steps` empty, and
  exit non-zero.
* A complete history must be ACCEPTED and reproduced.
  `test_a_complete_history_is_accepted_and_an_unchanged_component_reproduces_it`
  runs the same component on both sides and requires the recorded outcome to be
  reproduced with no difference and exit 0.
* A changed value must be NAMED, and the run must stop at it.
  `test_a_changed_pure_component_names_the_first_differing_step_and_exits_nonzero`
  and `test_the_first_difference_stops_the_re_run` require the first moved value
  and the absence of any later re-run.
* The command must have NO live effect authority.
  `test_an_impure_component_is_refused_with_the_purity_fact_and_not_executed`
  swaps in a component that requires a host and requires the purity refusal, and
  `test_the_history_is_not_written` requires the WAL to be byte-identical before
  and after a run.
* The determinism fact is real, not assumed.
  `test_a_history_without_a_determinism_record_is_refused` and
  `test_the_determinism_record_round_trips_and_consumes_no_seq`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import replay  # noqa: E402

from revl import simulate_swap  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.wal import determinism, read_wal  # noqa: E402

OLD = """service Counter {
  fn value() -> Int
  fn other() -> Int
}

component CounterC provides counter: Counter {
  provide counter {
    fn value() -> Int = 0
    fn other() -> Int = 5
  }
}
"""

NEW = """service Counter {
  fn value() -> Int
  fn other() -> Int
}

component CounterC provides counter: Counter {
  provide counter {
    fn value() -> Int = 1
    fn other() -> Int = 5
  }
}
"""

SAME_AS_OLD = OLD

NEW_WITH_TALLY = """service Counter {
  fn value() -> Int
  fn other() -> Int
}

service Tally {
  fn value() -> Int
}

component CounterC provides counter: Counter, tally: Tally {
  provide counter {
    fn value() -> Int = 0
    fn other() -> Int = 5
  }
  provide tally {
    fn value() -> Int = 0
  }
}
"""

IMPURE = """service Store {
  fn get(k: Str) -> Int
}

service Counter {
  fn value() -> Int
  fn other() -> Int
}

component CounterC provides counter: Counter requires host: Store {
  provide counter {
    fn value() -> Int = 0
    fn other() -> Int = 5
  }
}
"""

PARAMETERIZED = """service Counter {
  fn value(x: Int) -> Int
}

component CounterC provides counter: Counter {
  provide counter {
    fn value(x: Int) -> Int = x
  }
}
"""

ASYNC = """service Counter {
  async fn value() -> Int
  fn other() -> Int
}

component CounterC provides counter: Counter {
  provide counter {
    async fn value() -> Int = 0
    fn other() -> Int = 5
  }
}
"""


def _write(tmp_path, name, text):
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return str(target)


def _wal(tmp_path, crossings, *, determinism=True, complete=True,
         finished=True, run_complete=True, name="run.wal"):
    """One WAL with an admission decision and the named fenced crossings.

    `crossings` is a list of `(receiver, method, outcome)`; `complete=False`
    leaves every `begin` without its `complete`, which is the in-flight shape
    the preflight must refuse. `determinism=False` omits the record entirely.
    """
    path = str(tmp_path / name)
    wal = replay.WriteAheadLog(path, ir={}, generation=1).open()
    wal.record_admit_decided(decision_id="d1", turn={}, expected={},
                             spends=[], components=["CounterC"],
                             keys=["counter"])
    wal.begin_decision("d1")
    for receiver, method, outcome in crossings:
        ordinal = wal.record_fenced_crossing_begin(receiver=receiver,
                                                   method=method)
        if complete:
            wal.record_fenced_crossing_complete(ordinal=ordinal,
                                                outcome=outcome)
    wal.end_decision()
    if determinism:
        wal.record_determinism(seed=7, clock={"start": 1, "end": 2})
    if finished:
        wal.commit_activation(components=["CounterC"])
        if run_complete:
            wal.commit_run()
    wal.close()
    return path


def _swap(tmp_path, old=OLD, new=NEW, crossings=None, **wal_options):
    old_path = _write(tmp_path, "old.rvl", old)
    new_path = _write(tmp_path, "new.rvl", new)
    history = _wal(tmp_path, crossings if crossings is not None
                   else [("counter", "value", 0), ("counter", "other", 5)],
                   **wal_options)
    document = simulate_swap.swap(old_path, new_path, history)
    return document, history


def _facts(document):
    return [entry["fact"] for entry in document["missing"]]


# ---------------------------------------------------------------------------
# slice 1: the preflight names the missing fact and runs nothing
# ---------------------------------------------------------------------------


def test_a_history_without_a_recorded_host_response_is_refused_with_the_fact_named(
        tmp_path):
    document, _history = _swap(tmp_path, complete=False)

    assert document["replayable"] is False
    assert "hostResponses" in _facts(document)
    entry = next(m for m in document["missing"] if m["fact"] == "hostResponses")
    # the exact crossing, not just a count: a reader has to be able to find it
    assert entry["crossings"][0]["receiver"] == "counter"
    assert entry["crossings"][0]["method"] == "value"
    assert entry["crossings"][0]["ordinal"] == 0
    # the preflight runs nothing: no step was re-run and no value was invented
    assert document["steps"] == []
    assert document["firstDifference"] is None


def test_a_history_without_a_determinism_record_is_refused(tmp_path):
    document, _history = _swap(tmp_path, determinism=False)

    assert document["replayable"] is False
    assert "seedsAndClock" in _facts(document)
    assert document["steps"] == []


def test_an_unfinished_history_is_refused(tmp_path):
    document, _history = _swap(tmp_path, finished=False)

    assert document["replayable"] is False
    assert "complete" in _facts(document)
    assert document["complete"] is False
    assert document["steps"] == []


def test_a_torn_tail_is_refused(tmp_path):
    old_path = _write(tmp_path, "old.rvl", OLD)
    new_path = _write(tmp_path, "new.rvl", NEW)
    history = _wal(tmp_path, [("counter", "value", 0)])
    with open(history, "ab") as handle:
        handle.write(b'{"record": "admit-crossing"')

    document = simulate_swap.swap(old_path, new_path, history)

    assert document["replayable"] is False
    assert document["torn"] is True
    assert "complete" in _facts(document)
    assert document["steps"] == []


def test_a_history_without_a_run_complete_marker_is_refused(tmp_path):
    # activation finished, so `complete` alone would call this whole; the
    # missing shutdown marker is the steady-state kill whose crossings are a
    # prefix and whose absence has no torn tail to give it away.
    document, _history = _swap(tmp_path, run_complete=False)

    assert document["replayable"] is False
    assert "complete" in _facts(document)
    assert document["steps"] == []


def test_a_history_with_no_crossing_into_the_component_is_refused(tmp_path):
    document, _history = _swap(tmp_path,
                               crossings=[("elsewhere", "value", 0)])

    assert document["replayable"] is False
    assert "recordedSteps" in _facts(document)
    assert document["steps"] == []


def test_a_parameterized_method_is_refused_because_arguments_are_not_on_the_record(
        tmp_path):
    document, _history = _swap(
        tmp_path, old=PARAMETERIZED, new=PARAMETERIZED,
        crossings=[("counter", "value", 0)])

    assert document["replayable"] is False
    assert "callArgs" in _facts(document)
    entry = next(m for m in document["missing"] if m["fact"] == "callArgs")
    assert entry["crossings"][0]["params"] == ["x"]
    assert document["steps"] == []


def test_an_impure_component_is_refused_with_the_purity_fact_and_not_executed(
        tmp_path):
    document, _history = _swap(tmp_path, old=SAME_AS_OLD, new=IMPURE)

    assert document["replayable"] is False
    assert "purity" in _facts(document)
    entry = next(m for m in document["missing"] if m["fact"] == "purity")
    assert any("requires" in reason for reason in entry["new"])
    assert document["steps"] == []


def test_an_async_component_is_refused_by_the_purity_gate(tmp_path):
    document, _history = _swap(tmp_path, old=ASYNC, new=ASYNC)

    assert document["replayable"] is False
    entry = next(m for m in document["missing"] if m["fact"] == "purity")
    assert any("async" in reason for reason in entry["old"])
    assert document["steps"] == []


def test_an_impure_call_in_argument_position_is_refused_by_the_pure_walk():
    # the callee is pure, so checking only its body would let the impure
    # ARGUMENT through; the walk has to descend into the arguments too.
    functions = {
        "inc": {"name": "inc", "body": [
            {"step": "return", "expr": {"kind": "var", "name": "x"}}]},
    }
    impure = {
        "kind": "fn", "name": "inc",
        "args": [{"kind": "call",
                  "target": {"kind": "req", "name": "host"},
                  "method": "get", "args": []}],
    }
    pure = {"kind": "fn", "name": "inc",
            "args": [{"kind": "lit", "value": 1}]}

    reason = simulate_swap._pure_expression(impure, functions, frozenset())
    assert reason is not None
    assert "`call`" in reason
    assert simulate_swap._pure_expression(pure, functions, frozenset()) is None


def test_a_refused_history_is_not_executed(tmp_path, monkeypatch):
    old_path = _write(tmp_path, "old.rvl", OLD)
    new_path = _write(tmp_path, "new.rvl", NEW)
    history = _wal(tmp_path, [("counter", "value", 0)], complete=False)
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a refused history must not be executed")

    monkeypatch.setattr(simulate_swap, "_execute", spy)
    document = simulate_swap.swap(old_path, new_path, history)

    assert document["replayable"] is False
    assert calls == []


def test_a_refused_history_is_not_written(tmp_path):
    old_path = _write(tmp_path, "old.rvl", OLD)
    new_path = _write(tmp_path, "new.rvl", NEW)
    history = _wal(tmp_path, [("counter", "value", 0)], determinism=False)
    before = Path(history).read_bytes()
    files_before = sorted(p.name for p in tmp_path.iterdir())

    simulate_swap.swap(old_path, new_path, history)

    assert Path(history).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before


# ---------------------------------------------------------------------------
# slice 2: a complete history is accepted; a changed value is named; the run
# stops at the first difference
# ---------------------------------------------------------------------------


def test_a_complete_history_is_accepted_and_an_unchanged_component_reproduces_it(
        tmp_path):
    document, _history = _swap(tmp_path, new=SAME_AS_OLD)

    assert document["replayable"] is True
    assert document["missing"] == []
    assert document["firstDifference"] is None
    assert simulate_swap.ok(document) is True
    assert [step["same"] for step in document["steps"]] == [True, True]


def test_a_changed_pure_component_names_the_first_differing_step_and_exits_nonzero(
        tmp_path):
    document, _history = _swap(tmp_path)

    assert document["replayable"] is True
    first = document["firstDifference"]
    assert first is not None
    assert (first["receiver"], first["method"]) == ("counter", "value")
    assert first["recorded"] == 0
    assert first["value"] == 1
    assert first["kind"] == "value"
    assert simulate_swap.ok(document) is False


def test_the_first_difference_stops_the_re_run(tmp_path):
    document, _history = _swap(tmp_path)

    # value differs first; `other` never re-runs, and the report says so
    assert len(document["steps"]) == 1
    assert document["steps"][0]["method"] == "value"
    assert document["blastRadius"]["rerunSteps"] == 1
    assert document["blastRadius"]["unrunSteps"] == 1


def test_an_outcome_old_cannot_reproduce_is_reported_not_hidden(tmp_path):
    document, _history = _swap(tmp_path, crossings=[("counter", "value", 9)])

    first = document["firstDifference"]
    assert first is not None
    assert first["kind"] == "not-reproducible"
    assert first["recorded"] == 9
    assert first["value"] == 0
    assert simulate_swap.ok(document) is False


def test_a_key_new_adds_is_reported_not_raised(tmp_path):
    # the receiver is in the OLD|NEW union, so the preflight is replayable; OLD
    # cannot make the call, and that must be a reported step, not a traceback.
    document, _history = _swap(
        tmp_path, old=OLD, new=NEW_WITH_TALLY,
        crossings=[("tally", "value", 0)])

    first = document["firstDifference"]
    assert first is not None
    assert first["kind"] == "not-reproducible"
    assert "provides no key" in first["reason"]
    assert simulate_swap.ok(document) is False


def test_the_history_is_not_written(tmp_path):
    old_path = _write(tmp_path, "old.rvl", OLD)
    new_path = _write(tmp_path, "new.rvl", NEW)
    history = _wal(tmp_path, [("counter", "value", 0)])
    before = Path(history).read_bytes()
    files_before = sorted(p.name for p in tmp_path.iterdir())

    simulate_swap.swap(old_path, new_path, history)

    assert Path(history).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == files_before


# ---------------------------------------------------------------------------
# the CLI and the determinism record
# ---------------------------------------------------------------------------


def test_the_cli_reports_the_first_difference_and_exits_nonzero(tmp_path, capsys):
    from revl.__main__ import main  # noqa: PLC0415 - the surface under test

    old_path = _write(tmp_path, "old.rvl", OLD)
    new_path = _write(tmp_path, "new.rvl", NEW)
    history = _wal(tmp_path, [("counter", "value", 0)])

    code = main(["simulate", "swap", old_path, new_path,
                 "--history", history, "--json"])
    captured = capsys.readouterr()
    assert code == 1
    import json  # noqa: PLC0415
    document = json.loads(captured.out)
    assert document["firstDifference"]["method"] == "value"


def test_the_cli_accepts_a_complete_history_with_no_difference(tmp_path, capsys):
    from revl.__main__ import main  # noqa: PLC0415

    old_path = _write(tmp_path, "old.rvl", OLD)
    new_path = _write(tmp_path, "new.rvl", SAME_AS_OLD)
    history = _wal(tmp_path, [("counter", "value", 0)])

    code = main(["simulate", "swap", old_path, new_path,
                 "--history", history])
    captured = capsys.readouterr()
    assert code == 0
    assert "no difference" in captured.out


def test_the_determinism_record_round_trips_and_consumes_no_seq(tmp_path):
    # the writer and the offline reader cannot drift on the record name
    from revl import wal as wal_core  # noqa: PLC0415

    assert replay.RECORD_DETERMINISM == wal_core.RECORD_DETERMINISM
    history = _wal(tmp_path, [("counter", "value", 0)])
    records = read_wal(history)["records"]
    record = determinism(records)
    assert record["seed"] == 7
    assert record["clock"] == {"start": 1, "end": 2}
    # a fact about the run, not an effect: it takes no seq, so the crossing
    # sequence is unchanged by its presence
    assert "seq" not in record
    effects = [r for r in records if r.get("record") == "admit-crossing"]
    assert [r["seq"] for r in effects] == [1, 2]


# ---------------------------------------------------------------------------
# emitter refusal versus fault (issue #1406): a refusal is an answer, a fault
# stays loud -- the same three-way discipline every emitter entry point holds.
# ---------------------------------------------------------------------------

#: A document the py tier refuses by name (`validated` extern, issue #1382).
_REFUSED = """type Call = { tool: Str, args: Str }
type AgentTurn = Final(Str) | ToolCalls(List[Call])

extern emission[model] validated fn complete(h: Str) -> AgentTurn = @py {
  return {"kind": "Final", "value": "x"}
}

service Counter {
  fn value() -> Int
}

component CounterC provides counter: Counter {
  provide counter {
    fn value() -> Int = 0
  }
}
"""


def _fake_emitter(exc_factory):
    """A module whose `emit` raises what the factory builds, with its own
    `EmitError` (a `ValueError`) -- the two controls every assertion below is
    paired with."""
    import types  # noqa: PLC0415

    module = types.ModuleType("fake_emit")
    module.__file__ = "<fake_emit>"

    class EmitError(ValueError):
        pass

    module.EmitError = EmitError

    def emit(ir):
        raise exc_factory(module)

    module.emit = emit
    return module


def _refusing(message="this tier cannot lower that"):
    return _fake_emitter(lambda m: m.EmitError(message))


def _faulting():
    return _fake_emitter(
        lambda m: AttributeError("'NoneType' object has no attribute 'get'"))


def _bare_value_error():
    """THE control for this issue. `EmitError` inherits from `ValueError`, so a
    fix that catches `ValueError` passes every refusal assertion while
    swallowing a real fault. A bare `ValueError` is a fault, not a refusal."""
    return _fake_emitter(lambda m: ValueError("an emitter bug that is a ValueError"))


def test_the_re_run_reports_an_emitter_refusal_as_an_answer(monkeypatch):
    """A refusal is the tier's own sentence for this document, carried verbatim
    in the reported step -- not a traceback and not a swallowed pass."""
    from revl import placement  # noqa: PLC0415

    ir = compile_source(_REFUSED, "refused.rvl")
    monkeypatch.setattr(placement, "backend_emit_module", lambda tier: _refusing(
        "the py tier cannot lower this document"))
    with pytest.raises(simulate_swap.SwapError) as caught:
        simulate_swap._execute(ir, "CounterC", "counter", "value")
    assert "the py emitter refused the document" in str(caught.value)
    assert "the py tier cannot lower this document" in str(caught.value)


def test_the_real_py_refusal_reaches_the_report(monkeypatch):
    """The paired green half: the real emitter's own refusal sentence, read
    live off the refused document, not a fragment written down in this file."""
    ir = compile_source(_REFUSED, "refused.rvl")
    with pytest.raises(simulate_swap.SwapError) as caught:
        simulate_swap._execute(ir, "CounterC", "counter", "value")
    assert "the py emitter refused the document" in str(caught.value)
    assert "`validated` extern `complete`" in str(caught.value)


def test_the_re_run_lets_a_fault_through(monkeypatch):
    """The control, and the half that was wrong: a bug inside the emitter must
    keep its own traceback, not arrive as a sentence about the document."""
    from revl import placement  # noqa: PLC0415

    ir = compile_source(_REFUSED, "refused.rvl")
    monkeypatch.setattr(placement, "backend_emit_module", lambda tier: _faulting())
    with pytest.raises(AttributeError, match="NoneType"):
        simulate_swap._execute(ir, "CounterC", "counter", "value")


def test_the_re_run_lets_a_bare_value_error_through(monkeypatch):
    """The second control, and the one that fails against the lazy fix. An
    emitter raising a bare `ValueError` is faulting, not refusing, even though
    `EmitError` is a `ValueError` too."""
    from revl import placement  # noqa: PLC0415

    ir = compile_source(_REFUSED, "refused.rvl")
    monkeypatch.setattr(placement, "backend_emit_module",
                        lambda tier: _bare_value_error())
    with pytest.raises(ValueError, match="an emitter bug"):
        simulate_swap._execute(ir, "CounterC", "counter", "value")
