"""The UI transaction unit RUNS its LIFO compensations on the python tier
(roadmap item 522 slice 3, issue #1369, `docs/design/538-ui-transactions.md`
§10).

WHAT WAS WRONG. `tests/test_ui_transaction_run_1369.py` pins the run revl
COMPUTES: on item 522's five-step transaction, a failure detected at the third
step's postcondition runs the compensations of steps two and one, in that
order. Nothing performed it. Measured on the python tier before this change,
with the same program and a fake desktop that logs every host body it runs:

  * a provide method never registered an extern's DECLARED `compensate` at
    all. The emitter handled only a site-spelled `emit ... compensate ...`, and
    a computer-use extern declares its inverse on the extern, which is the only
    form item 522 slice 1 checks. So the entries the static run names did not
    exist at runtime;
  * nothing was keyed on the failure. A method-registered entry is parked on
    the activation frame until the session settles, and a clean unload after a
    failed call DISCHARGES it: the transaction stops half way, the fields it
    typed stay typed, and no compensation ever runs.

WHAT RUNS NOW. A provide method that crosses a computer-use verb is one unit,
the same unit `ui_transaction.method_plan` reads. If the call fails, the unit
settles the entries it registered, witnessed inverses first and compensations
second, each newest first, and the failure propagates. The compensating host
bodies are still the substrate's; the order and the membership are revl's.

The oracle below compares the RUNTIME run against the STATIC run the erase
report prints for the same program, so the two cannot drift apart silently.

Every test here drives a live cordis-py composition. Without the pinned
`cordis` fork installed they SKIP, and a skip is not a pass.
"""

from __future__ import annotations

import contextvars
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import ui_transaction as uitx  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the unit runs against a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`",
)

UI_TARGET = """
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}
"""


def _host(line: str, ret: str = "None") -> str:
    """A fake-desktop host body. It logs `line` (a Python expression) to
    `REVL_UI_LOG` and raises when `REVL_UI_FAIL` names this line and the
    occurrence it has reached (`locate:Approve#2` is the second resolution of
    `Approve`)."""
    return (
        "@py {\n"
        "    import os\n"
        f"    line = {line}\n"
        "    with open(os.environ['REVL_UI_LOG'], 'a', encoding='utf-8') as f:\n"
        "        f.write(line + chr(10))\n"
        "    want, _, nth = os.environ.get('REVL_UI_FAIL', '').partition('#')\n"
        "    if line == want:\n"
        "        with open(os.environ['REVL_UI_LOG'], encoding='utf-8') as f:\n"
        "            seen = [x for x in f.read().splitlines() if x == want]\n"
        "        if len(seen) == int(nth or '1'):\n"
        "            raise RuntimeError('the substrate could not confirm ' + line)\n"
        f"    return {ret}\n"
        "}"
    )


_TARGET = ("{'application': 'Billing', 'window': 'w', 'role': 'r', "
           "'name': name, 'evidence': 'e', 'action': 'a', 'session': 's', "
           "'bounds': 'b', 'expiry': 0, 'confirm': False}")

#: The declarations of `tests/test_ui_transaction_run_1369.py`, with host
#: bodies that log. Two compensatable steps with declared inverses, a
#: `ui.click` (no inverse exists), another compensatable step and a
#: `ui.download` (irreversible).
DECLARATIONS = f"""
extern emission[screen.observe] fn read_pane(region: Str) -> Str
  = {_host("'observe'", "'pane'")}
extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget
  = {_host("'locate:' + name", _TARGET)}
extern pure fn clear_amount() = {_host("'compensate:clear_amount'")}
extern pure fn clear_memo() = {_host("'compensate:clear_memo'")}
extern pure fn clear_note() = {_host("'compensate:clear_note'")}
extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = {_host("'type_amount'")}
extern emission[ui.text] fn type_memo(target: UiTarget, s: Str)
  compensate clear_memo()
  = {_host("'type_memo'")}
extern emission[ui.click] fn actuate(target: UiTarget) = {_host("'actuate'")}
extern emission[ui.text] fn type_note(target: UiTarget, s: Str)
  compensate clear_note()
  = {_host("'type_note'")}
extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str
  = {_host("'fetch_receipt'", "'receipt'")}
"""

#: Item 522's five-step transaction, as `test_ui_transaction_run_1369.py`
#: writes it. The read after `actuate` resolves `Approve` again off a fresh
#: observation, so it is the one bound postcondition (slice 5).
FIVE_STEPS = UI_TARGET + DECLARATIONS + """
service Ops { emission fn run(region: Str) -> Int }
component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      let pane1 = emit read_pane(region)
      let amount = emit locate(pane1, "Amount")
      emit type_amount(amount, "10")
      let pane2 = emit read_pane(region)
      let memo = emit locate(pane2, "Memo")
      emit type_memo(memo, "m")
      let pane3 = emit read_pane(region)
      let approve = emit locate(pane3, "Approve")
      emit actuate(approve)
      let pane4 = emit read_pane(region)
      let checked = emit locate(pane4, "Approve")
      let pane5 = emit read_pane(region)
      let note = emit locate(pane5, "Note")
      emit type_note(note, "n")
      let pane6 = emit read_pane(region)
      let receipt = emit locate(pane6, "Attach")
      let saved = emit fetch_receipt(receipt)
      return 1
    }
  }
}
"""

#: The postcondition read that checks `actuate`: the second resolution of
#: `Approve`. Its failure is how the transaction learns the click did not take.
POSTCONDITION_UNMET = "locate:Approve#2"

#: The forward crossings the five-step program makes before that read fails.
FORWARD_TO_THE_CHECK = [
    "observe", "locate:Amount", "type_amount",
    "observe", "locate:Memo", "type_memo",
    "observe", "locate:Approve", "actuate",
    "observe", "locate:Approve",
]


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    log = tmp_path / "desktop.log"
    monkeypatch.setenv("REVL_UI_LOG", str(log))
    monkeypatch.delenv("REVL_UI_FAIL", raising=False)

    def lines() -> list[str]:
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return lines


def _session(source: str):
    from revl.mcp.session import Session
    session = Session()
    session.load(compile_source(source, "runtime_1369.rvl"))
    driver = session._driver
    ((_name, fiber),) = driver.fibers.items()
    frame = driver.runtime._frame_for_ctx(fiber.ctx)
    assert frame is not None, "no activation frame for the component"
    return session, frame


def _call_failing(session, *args):
    with pytest.raises(Exception) as caught:
        session.call("ops", "run", list(args))
    return caught.value


def _compensations(lines: list[str]) -> list[str]:
    return [line.split(":", 1)[1] for line in lines
            if line.startswith("compensate:")]


# ------------------------------------------------------------ the oracle


@needs_cordis
def test_the_five_step_oracle_runs_on_the_python_tier(desktop, monkeypatch):
    """538 §10 performed, not computed: the third step's postcondition is
    unmet, and the compensations of steps two and one RUN, in that order, and
    nothing of steps four and five ever crosses."""
    monkeypatch.setenv("REVL_UI_FAIL", POSTCONDITION_UNMET)
    session, frame = _session(FIVE_STEPS)
    error = _call_failing(session, "r")
    assert "could not confirm locate:Approve" in str(error)
    assert desktop() == FORWARD_TO_THE_CHECK + [
        "compensate:clear_memo", "compensate:clear_amount"]
    session.unload()
    # the commit after the failed call does not run them again
    assert _compensations(desktop()) == ["clear_memo", "clear_amount"]


@needs_cordis
def test_the_runtime_run_is_the_run_the_erase_report_prints(desktop, monkeypatch):
    """The static run for `actuate` (the one bound postcondition) and the run
    the python tier performed name the same compensations in the same order.
    If either side changes, this is the test that says they disagree."""
    static = uitx.plans(compile_source(FIVE_STEPS, "runtime_1369.rvl"))[0]
    [computed] = static["compensationRuns"]
    assert computed["failedStep"] == "actuate"

    monkeypatch.setenv("REVL_UI_FAIL", POSTCONDITION_UNMET)
    session, frame = _session(FIVE_STEPS)
    _call_failing(session, "r")
    [performed] = frame.ui_transaction_runs
    assert [entry["step"] for entry in performed["ran"]] == computed["ran"] \
        == ["type_memo", "type_amount"]
    assert [entry["compensation"] for entry in performed["ran"]] == \
        ["clear_memo", "clear_amount"]
    # the step revl could not see land has no inverse, so nothing ran for it
    assert "actuate" not in [entry["step"] for entry in performed["ran"]]
    # the read that raised is where the call stopped, and it is the read the
    # static plan names as the one that checks `actuate`
    checked_by = {s["extern"]: s.get("postconditionCheckedBy")
                  for s in static["steps"]}
    assert performed["failedStep"] == checked_by["actuate"] == "locate"
    assert performed["crossed"][-1] == "locate"
    assert "type_note" not in performed["crossed"]
    assert "fetch_receipt" not in performed["crossed"]
    assert performed["residue"] == []
    session.unload()


@needs_cordis
def test_before_the_failure_nothing_is_compensated(desktop):
    """The control. A call that completes runs no compensation, and the clean
    unload after it DISCHARGES every entry: the typed fields are the
    deliverable. So the compensations in the oracle are caused by the failure
    and not by the unit merely existing."""
    session, frame = _session(FIVE_STEPS)
    assert session.call("ops", "run", ["r"])["result"] == 1
    assert _compensations(desktop()) == []
    assert frame.ui_transaction_runs == []
    assert len(frame._deferred_compensations) == 3
    session.unload()
    assert _compensations(desktop()) == []
    assert all(entry.discharged for entry in frame._compensations)


# ---------------------------------------------------- the rules the run keeps


@needs_cordis
def test_the_failing_crossings_own_compensation_runs(desktop, monkeypatch):
    """538 §10's rule for the failing step, at runtime: `type_memo` raised, so
    revl could not see whether the typing landed, and its declared inverse runs
    first. `ui_transaction.compensation_run` states the same rule statically."""
    monkeypatch.setenv("REVL_UI_FAIL", "type_memo")
    session, frame = _session(FIVE_STEPS)
    _call_failing(session, "r")
    assert _compensations(desktop()) == ["clear_memo", "clear_amount"]
    [performed] = frame.ui_transaction_runs
    assert performed["failedStep"] == "type_memo"
    static = uitx.compensation_run(
        [("type_amount", "ui.text", True), ("type_memo", "ui.text", True),
         ("actuate", "ui.click", False)], "type_memo")
    assert [entry["step"] for entry in performed["ran"]] == static["ran"]
    session.unload()


@needs_cordis
def test_a_failure_before_any_compensatable_step_runs_nothing(desktop, monkeypatch):
    monkeypatch.setenv("REVL_UI_FAIL", "locate:Amount")
    session, frame = _session(FIVE_STEPS)
    _call_failing(session, "r")
    assert desktop() == ["observe", "locate:Amount"]
    [performed] = frame.ui_transaction_runs
    assert performed["ran"] == []
    session.unload()


@needs_cordis
def test_a_failing_compensation_is_residue_and_the_older_one_still_runs(
        desktop, monkeypatch):
    """continue-and-record, the teardown contract's Phase-2 rule: `clear_memo`
    raises, `clear_amount` still runs, the failure is a `compensation-residue`
    record, and the call still raises the SUBSTRATE's error, not the
    compensation's. A compensation that ran and failed is recorded in the
    merged residue schema that already exists; no sixth residue state is
    invented for it."""
    monkeypatch.setenv("REVL_UI_FAIL", POSTCONDITION_UNMET)
    source = FIVE_STEPS.replace(
        "extern pure fn clear_memo() = " + _host("'compensate:clear_memo'"),
        "extern pure fn clear_memo() = @py {\n"
        "    import os\n"
        "    with open(os.environ['REVL_UI_LOG'], 'a', encoding='utf-8') as f:\n"
        "        f.write('compensate:clear_memo' + chr(10))\n"
        "    raise RuntimeError('the memo field is gone')\n"
        "}")
    assert source != FIVE_STEPS
    session, frame = _session(source)
    error = _call_failing(session, "r")
    assert "could not confirm locate:Approve" in str(error)
    assert _compensations(desktop()) == ["clear_memo", "clear_amount"]
    [performed] = frame.ui_transaction_runs
    assert [(e["step"], e["failed"]) for e in performed["ran"]] == [
        ("type_memo", True), ("type_amount", False)]
    [residue] = performed["residue"]
    assert residue["kind"] == "compensation-residue"
    assert residue["outcome"] == "failed"
    assert residue["error"]["message"] == "the memo field is gone"
    assert residue in frame.compensation_residue
    session.unload()


@needs_cordis
def test_a_later_abort_does_not_run_a_settled_unit_twice(desktop, monkeypatch):
    """The unit takes its entries off the frame's deferred lists before it
    runs them, so a session abort after the failed call does not reach them
    again. A second, successful call's entries are still the abort's to run."""
    monkeypatch.setenv("REVL_UI_FAIL", POSTCONDITION_UNMET)
    session, frame = _session(FIVE_STEPS)
    _call_failing(session, "r")
    assert _compensations(desktop()) == ["clear_memo", "clear_amount"]
    monkeypatch.delenv("REVL_UI_FAIL")
    assert session.call("ops", "run", ["r"])["result"] == 1
    frame.abort()
    session.unload()
    assert _compensations(desktop()) == [
        "clear_memo", "clear_amount",                   # the failed call's unit
        "clear_note", "clear_memo", "clear_amount"]     # the abort, LIFO


# --------------------------------------- a crossing wherever it is written


TAIL = UI_TARGET + DECLARATIONS + """
service Ops { emission fn run(region: Str) -> Int }
component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      let pane = emit read_pane(region)
      let amount = emit locate(pane, "Amount")
      let typed = emit type_amount(amount, "10")
      let approve = emit locate(pane, "Approve")
      return emit actuate(approve)
    }
  }
}
"""


@needs_cordis
def test_a_let_bound_crossing_registers_its_inverse(desktop, monkeypatch):
    """Issue #1327's lesson, at runtime. `let typed = emit type_amount(...)`
    is not a bare `emit` statement, and the tail-position `return emit
    actuate(...)` is not one either. The compensation is registered at the
    extern, so it exists wherever the crossing is written, and the tail
    crossing's failure is still the unit's failure."""
    monkeypatch.setenv("REVL_UI_FAIL", "actuate")
    session, frame = _session(TAIL)
    _call_failing(session, "r")
    assert desktop() == ["observe", "locate:Amount", "type_amount",
                         "locate:Approve", "actuate", "compensate:clear_amount"]
    [performed] = frame.ui_transaction_runs
    assert performed["failedStep"] == "actuate"
    assert [entry["step"] for entry in performed["ran"]] == ["type_amount"]
    session.unload()


ASYNC = UI_TARGET + f"""
extern emission[ui.find] async fn locate(pane: Str, name: Str) -> UiTarget
  = {_host("'locate:' + name", _TARGET)}
extern pure fn clear_amount() = {_host("'compensate:clear_amount'")}
extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = {_host("'type_amount'")}
extern emission[ui.click] async fn actuate(target: UiTarget) = {_host("'actuate'")}
service Ops {{ async emission fn run(region: Str) -> Int }}
component Agent provides ops: Ops {{
  provide ops {{
    async fn run(region) {{
      let amount = emit locate(region, "Amount")
      emit type_amount(amount, "10")
      let approve = emit locate(region, "Approve")
      emit actuate(approve)
      return 1
    }}
  }}
}}
"""


@needs_cordis
def test_an_async_method_and_async_crossings_are_one_unit(desktop, monkeypatch):
    """An `async` provide method is a unit too, and an awaited computer-use
    crossing that raises is the step it failed at. (An async extern may not
    declare `compensate` at all yet, so the compensatable step is sync.)"""
    monkeypatch.setenv("REVL_UI_FAIL", "actuate")
    session, frame = _session(ASYNC)
    _call_failing(session, "r")
    assert desktop() == ["locate:Amount", "type_amount", "locate:Approve",
                         "actuate", "compensate:clear_amount"]
    [performed] = frame.ui_transaction_runs
    assert performed["failedStep"] == "actuate"
    session.unload()


# ------------------------------------------------ proof inverse, then offset


WITNESSED = UI_TARGET + DECLARATIONS + """
type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py {
    import os
    with open(os.environ['REVL_UI_LOG'], 'a', encoding='utf-8') as f:
        f.write('unstash' + chr(10))
    os.replace(w['bak'], w['path'])
    return
}
extern witnessed[fs] fn stash(p: Str) -> Result[Stash, FsError]
  undo unstash(result) = @py {
    import os
    os.replace(p, p + '.bak')
    return Ok({'path': p, 'bak': p + '.bak'})
}
service Ops { emission fn run(p: Str) -> Int }
component Agent provides ops: Ops {
  provide ops {
    fn run(p) {
      let pane = emit read_pane(p)
      let amount = emit locate(pane, "Amount")
      emit type_amount(amount, "10")
      effect stash(p)
      let approve = emit locate(pane, "Approve")
      emit actuate(approve)
      return 1
    }
  }
}
"""


@needs_cordis
def test_the_witnessed_inverse_replays_before_the_compensation(
        desktop, monkeypatch, tmp_path):
    """The teardown contract's two phases, scoped to the unit: the proof
    inverse first, the best-effort offset second, even though the offset's step
    came first. The file the call moved is back where it was."""
    target = tmp_path / "artifact.txt"
    target.write_text("deliverable", encoding="utf-8")
    monkeypatch.setenv("REVL_UI_FAIL", "actuate")
    session, frame = _session(WITNESSED)
    _call_failing(session, str(target))
    assert desktop()[-2:] == ["unstash", "compensate:clear_amount"]
    assert target.exists() and not Path(str(target) + ".bak").exists()
    [performed] = frame.ui_transaction_runs
    assert performed["replayed"] == ["unstash"]
    assert frame._deferred_transactional == []
    session.unload()
    assert desktop().count("unstash") == 1


# ----------------------------------------------------- state it must restore


@needs_cordis
def test_the_unit_leaves_no_task_state_behind(desktop, monkeypatch):
    """The unit is a context variable, set on entry and reset on every exit.
    A leak would make the NEXT call's crossings register onto a unit that has
    already settled."""
    session, frame = _session(FIVE_STEPS)
    runtime = session._driver.runtime   # the module the emitted code imports
    before = runtime._UI_UNIT.get()
    assert session.call("ops", "run", ["r"])["result"] == 1
    assert runtime._UI_UNIT.get() is before is None
    # the clean call already resolved `Approve` twice; its postcondition read
    # in the second call is the fourth
    monkeypatch.setenv("REVL_UI_FAIL", "locate:Approve#4")
    _call_failing(session, "r")
    assert runtime._UI_UNIT.get() is None
    assert contextvars.copy_context().get(runtime._UI_UNIT) is None
    session.unload()


def test_a_program_with_no_computer_use_verb_is_emitted_as_before():
    """The unit and the decorator are emitted only for a computer-use program.
    The goldens (`tools/regen_goldens.py --check`) hold the same property for
    every other document in the tree."""
    from _backend_import import backend_emitter
    emit = backend_emitter("python")
    plain = compile_source(
        "extern emission fn note(msg: Str) -> Unit = @py { return }\n"
        "extern pure fn off() = @py { return None }\n"
        "service Ops { emission fn run(msg: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn run(msg) { emit note(msg) compensate off() } }\n"
        "}\n", "plain_1369.rvl")
    code = emit.emit(plain)
    assert "ui_transaction" not in code
    assert "ui_crossing" not in code
    ui = emit.emit(compile_source(FIVE_STEPS, "runtime_1369.rvl"))
    assert "with _revl_frame.ui_transaction('ops.run'):" in ui
    assert "@_revl_ui_crossing('type_amount', lambda: clear_amount())" in ui
    assert "@_revl_ui_crossing('actuate', None)" in ui
